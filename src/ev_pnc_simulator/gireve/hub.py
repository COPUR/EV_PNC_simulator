"""GIREVE IOP (OCPI 2.2.1) as an eMSP sees it. Behaviour comes from the pinned profile, not from this file.

What the profile gives us (gireve-ocpi-2.2.1.profile.yaml): registration is started by the IOP
(Register-FromIOP, never by the eMSP), tokens are Base64 on every call, module calls carry routing and tracing
headers (credentials and versions do not), tokens are pushed with a full PUT in upper-case hex, only
START_SESSION and STOP_SESSION are supported, pulls are paged at 100 and tariffs cover at most a month.

The mock *serves* what an eMSP calls on the IOP (versions, locations, tariffs, tokens, commands) and *drives*
what the IOP calls on the eMSP (registration, authorize, pushes), see driver.py.
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from dataclasses import dataclass, field

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response

from .. import contracts, ocpi
from ..mockcore import Mock
from . import fixtures

PROFILE_FILE = "gireve-ocpi-2.2.1.profile.yaml"
VERSION = "2.2.1"
COMMANDS = ("START_SESSION", "STOP_SESSION", "RESERVE_NOW", "CANCEL_RESERVATION", "UNLOCK_CONNECTOR")
UID_FORMAT = re.compile(r"^[0-9A-F]+$")
EMAID = re.compile(r"^[A-Z]{2}[*-]?[A-Z0-9]{3}[*-]?C[A-Z0-9]{8}[*-]?[0-9A-Z]$")
TOKEN_REQUIRED = (
    "country_code",
    "party_id",
    "uid",
    "type",
    "contract_id",
    "issuer",
    "valid",
    "whitelist",
    "last_updated",
)


@dataclass
class Registration:
    registered: bool = False
    iop_token: str | None = None  # what the eMSP must present when it calls the IOP
    emsp_token: str | None = None  # what the IOP presents when it calls the eMSP
    emsp_endpoints: dict[str, str] = field(default_factory=dict)  # identifier -> url, from the eMSP's version details
    emsp_party: tuple[str, str] = (fixtures.EMSP_COUNTRY, fixtures.EMSP_PARTY)


class Hub:
    def __init__(self, mock: Mock, *, base_url: str, client: httpx.AsyncClient, auto_callbacks: bool = True) -> None:
        self.mock, self.base_url, self.client, self.auto_callbacks = mock, base_url.rstrip("/"), client, auto_callbacks
        self.profile = contracts.profile(PROFILE_FILE)
        self.reset()

    def reset(self) -> None:
        self.reg = Registration()
        self.tokens: dict[tuple[str, str, str], dict] = {}
        self.commands: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.locations = fixtures.locations()
        self.tariffs = fixtures.tariffs()

    # ---- profile-derived facts -----------------------------------------------------------------------------------
    @property
    def supported_commands(self) -> set[str]:
        return set(self.profile["commands"]["supported_cpo_side"])

    @property
    def page_limit(self) -> int:
        return int(self.profile["tariffs"]["pull_limits"]["max_per_page"])

    @property
    def max_range(self):
        return ocpi.range_from_text(self.profile["tariffs"]["pull_limits"]["max_date_range"])

    def advertised_modules(self) -> list[tuple[str, str]]:
        """(identifier, role) the IOP advertises to an eMSP: supported modules minus those an eMSP cannot use."""
        supported = set(self.profile["modules"]["supported"])
        roles = {
            "credentials": "SENDER",
            "locations": "SENDER",
            "tariffs": "SENDER",
            "tokens": "RECEIVER",
            "commands": "RECEIVER",
        }
        return [(m, r) for m, r in roles.items() if m in supported]

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    # ---- request pipeline ------------------------------------------------------------------------------------------
    def serve(self, key: str, *, module: bool, fn):
        """Wrap a handler: log, rules, authentication, headers, then the behaviour in `fn`."""

        async def handle(request: Request) -> Response:
            raw = await request.body()
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                entry = self.mock.record(request, key, request.path_params, raw.decode(errors="replace"), None)
                return self.mock.finish(
                    entry,
                    ocpi.reply(
                        request, None, status_code=ocpi.GENERIC_CLIENT_ERROR, message="body is not JSON", http=400
                    ),
                )
            params = dict(request.path_params)
            rule = self.mock.rules.take({key}, body if isinstance(body, dict) else {}, params)
            entry = self.mock.record(request, key, params, body, rule)
            early = await self.mock.pre_response(rule)
            if early is not None:
                return self.mock.finish(entry, early)
            if rule and "ocpi_status" in rule.do:
                return self.mock.finish(
                    entry,
                    ocpi.reply(
                        request, None, status_code=rule.do["ocpi_status"], message=rule.do.get("message", "injected")
                    ),
                )
            denied = self.authenticate(request)
            if denied is not None:
                return self.mock.finish(entry, denied)
            if module:
                missing = ocpi.missing_headers(request, ocpi.ROUTING + ocpi.TRACING)
                if missing:
                    return self.mock.finish(
                        entry,
                        ocpi.reply(
                            request,
                            None,
                            status_code=ocpi.INVALID_PARAMETERS,
                            message="missing headers: " + ", ".join(missing),
                        ),
                    )
            response = fn(request, body, params, rule)
            uid = getattr(request.state, "command_uid", None)
            if uid and self.auto_callbacks:
                response.background = BackgroundTask(self.deliver_result, uid)
            return self.mock.finish(entry, response)

        return handle

    def authenticate(self, request: Request) -> Response | None:
        token = ocpi.decode_token(request.headers.get("authorization"))
        if self.reg.iop_token is not None and token == self.reg.iop_token:
            return None
        why = (
            "token is not Base64 encoded"
            if token is None and request.headers.get("authorization")
            else "unknown or missing token"
        )
        return ocpi.reply(request, None, status_code=ocpi.GENERIC_CLIENT_ERROR, message=why, http=401)

    # ---- served operations -----------------------------------------------------------------------------------------
    def versions(self, request, body, params, rule) -> Response:
        return ocpi.reply(request, [{"version": VERSION, "url": self.url(f"/ocpi/{VERSION}")}])

    def version_details(self, request, body, params, rule) -> Response:
        endpoints = [
            {"identifier": i, "role": r, "url": self.url(f"/ocpi/{VERSION}/{i}")} for i, r in self.advertised_modules()
        ]
        return ocpi.reply(request, {"version": VERSION, "endpoints": endpoints})

    def credentials(self, request, body, params, rule) -> Response:
        if request.method != "GET":
            return ocpi.reply(
                request,
                None,
                status_code=ocpi.GENERIC_CLIENT_ERROR,
                message="credentials update is not implemented: unregister, then register again",
                http=405,
            )
        return ocpi.reply(
            request, {"token": self.reg.iop_token, "url": self.url("/ocpi/versions"), "roles": [self.hub_role()]}
        )

    def hub_role(self) -> dict:
        return {
            "role": "HUB",
            "party_id": fixtures.HUB_PARTY,
            "country_code": fixtures.HUB_COUNTRY,
            "business_details": {"name": "GIREVE mock"},
        }

    def locations_get(self, request, body, params, rule) -> Response:
        return self._pull(request, self.locations, limited_range=False)

    def tariffs_get(self, request, body, params, rule) -> Response:
        return self._pull(request, self.tariffs, limited_range=True)

    def _pull(self, request: Request, rows: list[dict], *, limited_range: bool) -> Response:
        rows, problem = ocpi.window(request, rows, max_range=self.max_range if limited_range else None)
        if problem:
            return ocpi.reply(request, None, status_code=ocpi.INVALID_PARAMETERS, message=problem)
        try:
            page, headers = ocpi.paginate(request, rows, max_limit=self.page_limit)
        except ValueError:
            return ocpi.reply(
                request, None, status_code=ocpi.INVALID_PARAMETERS, message="offset and limit must be integers"
            )
        return ocpi.reply(request, page, headers=headers)

    def not_implemented(self, request, body, params, rule) -> Response:
        return ocpi.reply(
            request,
            None,
            status_code=ocpi.GENERIC_CLIENT_ERROR,
            message="not implemented by the IOP (see the GIREVE profile)",
            http=405,
        )

    def token_put(self, request, body, params, rule) -> Response:
        def refuse(message: str) -> Response:
            return ocpi.reply(request, None, status_code=ocpi.INVALID_PARAMETERS, message=message)

        if not isinstance(body, dict):
            return refuse("token object expected")
        missing = [f for f in TOKEN_REQUIRED if f not in body]
        if missing:
            return refuse("missing fields: " + ", ".join(missing))
        if (body["country_code"], body["party_id"], body["uid"]) != (
            params["country_code"],
            params["party_id"],
            params["token_uid"],
        ):
            return refuse("country_code, party_id and uid must equal the URL")
        if body["type"] not in ("RFID", "AD_HOC_USER", "APP_USER", "OTHER") or (
            body["type"] == "OTHER" and EMAID.match(body["uid"])
        ):
            return refuse("Plug&Charge (eMAID) tokens are not pushed; they are authorized in real time")
        if body["type"] == "RFID":
            uid = body["uid"]
            if (
                not UID_FORMAT.match(uid)
                or len(uid) % 2
                or len(uid) // 2 not in self.profile["tokens"]["rfid_uid"]["bytes"]
            ):
                return refuse("RFID uid must be upper-case hex of 4 or 7 bytes")
        key = (body["country_code"], body["party_id"], body["uid"])
        created = key not in self.tokens
        self.tokens[key] = body
        return ocpi.reply(request, None, http=201 if created else 200)

    def command(self, request, body, params, rule) -> Response:
        name = params["command"]
        if name not in COMMANDS:
            return ocpi.reply(request, None, status_code=ocpi.INVALID_PARAMETERS, message=f"unknown command {name}")
        if name not in self.supported_commands:
            return ocpi.reply(request, {"result": "NOT_SUPPORTED", "timeout": 30, "message": []})
        if not isinstance(body, dict) or "response_url" not in body:
            return ocpi.reply(request, None, status_code=ocpi.INVALID_PARAMETERS, message="response_url is required")
        if name == "START_SESSION":
            problem = self._start_problem(request, body)
            if problem:
                return problem
            sid = str(uuid.uuid4())
            self.sessions[sid] = {"id": sid, "location_id": body["location_id"], "evse_uid": body["evse_uid"]}
        elif body.get("session_id") not in self.sessions:
            return ocpi.reply(request, {"result": "UNKNOWN_SESSION", "timeout": 30, "message": []})
        uid = str(uuid.uuid4())
        outcome = (rule.do.get("command_result") if rule else None) or "ACCEPTED"
        self.commands[uid] = {"uid": uid, "command": name, "request": body, "result": outcome, "delivered": None}
        request.state.command_uid = uid
        return ocpi.reply(request, {"result": "ACCEPTED", "timeout": 30, "message": []})

    def _start_problem(self, request: Request, body: dict) -> Response | None:
        evse_required = self.profile["commands"]["start_session"]["evse_uid"] == "required"
        missing = [f for f in ("token", "location_id", *(("evse_uid",) if evse_required else ())) if not body.get(f)]
        if missing:
            return ocpi.reply(
                request, None, status_code=ocpi.INVALID_PARAMETERS, message="missing fields: " + ", ".join(missing)
            )
        loc = next((location for location in self.locations if location["id"] == body["location_id"]), None)
        if loc is None or body.get("evse_uid") not in {e["uid"] for e in loc["evses"]}:
            return ocpi.reply(request, None, status_code=ocpi.UNKNOWN_LOCATION, message="unknown location or evse_uid")
        token = body["token"]
        if (token.get("country_code"), token.get("party_id"), token.get("uid")) not in self.tokens:
            return ocpi.reply(request, None, status_code=ocpi.UNKNOWN_TOKEN, message="token was not pushed to the IOP")
        return None

    # ---- the asynchronous command result ---------------------------------------------------------------------------------
    async def deliver_result(self, uid: str, result: str | None = None) -> dict:
        """POST the CommandResult to the eMSP's response_url, as the CPO side does after the charge point answered."""
        cmd = self.commands[uid]
        outcome = result or cmd["result"]
        headers = {
            "Authorization": ocpi.encode_token(self.reg.emsp_token or ""),
            "X-Request-ID": str(uuid.uuid4()),
            "X-Correlation-ID": str(uuid.uuid4()),
            "OCPI-from-country-code": fixtures.CPO_COUNTRY,
            "OCPI-from-party-id": fixtures.CPO_PARTY,
            "OCPI-to-country-code": self.reg.emsp_party[0],
            "OCPI-to-party-id": self.reg.emsp_party[1],
        }
        try:
            resp = await self.client.post(
                cmd["request"]["response_url"], json={"result": outcome, "message": []}, headers=headers
            )
            cmd["delivered"] = {"result": outcome, "status": resp.status_code}
        except httpx.HTTPError as e:
            cmd["delivered"] = {"result": outcome, "error": str(e)}
        return cmd["delivered"]

    @staticmethod
    def new_token() -> str:
        return secrets.token_hex(16)
