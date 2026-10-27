"""OICP 2.3 EMP side of the Hubject mock, built from the pinned contract.

Roles. The contract is the EMP's view. Operations an EMP sends to Hubject (remote start and stop, pulls,
pushes) are *served* by the mock. Operations Hubject sends to the EMP (authorize, CDR, reservation stop,
notifications) are *driven*: the mock calls the EMP under test, see `driver.py`. Every operation in the
contract is in one of the two groups; a test checks that.

Business failures follow the contract: HTTP 200, `Result` false (or `AuthorizationStatus` NotAuthorized)
and a StatusCode. Bad input gets HTTP 400 with StatusCode 022.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import contracts, schema
from ..mockcore import Mock
from ..statuscodes import oicp_status_codes, success_status
from .state import Evse, World, iso

EMP_FILE = "hubject-oicp-2.3-emp.openapi.yaml"
PROFILE_FILE = "hubject-oicp-2.3-emp.profile.yaml"

# Short names for rules and the driver. A new operation in the contract fails a test until it is named here.
KEYS = {
    "eRoamingAuthorizeStart_v2.1": "authorize-start",
    "eRoamingAuthorizeStop_V2.1": "authorize-stop",
    "eRoamingAuthorizeRemoteStart_v2.1": "remote-start",
    "eRoamingAuthorizeRemoteStop_v2.1": "remote-stop",
    "eRoamingChargeDetailRecord_V2.2": "cdr-push",
    "eRoamingGetChargeDetailRecords_V2.2": "cdr-pull",
    "eRoamingPushAuthenticationData_V2.1": "auth-data-push",
    "eRoamingAuthorizeRemoteReservationStart_V1.1": "reservation-start",
    "eRoamingAuthorizeRemoteReservationStop_V1": "reservation-stop",
    "eRoamingPullEvseData_V2.3": "evse-data-pull",
    "eRoamingPullEvseStatus_V2.1": "evse-status-pull",
    "eRoamingPullPricingProductData_V1.0": "pricing-products-pull",
    "eRoamingPullEVSEPricing_V1.0": "evse-pricing-pull",
    "eRoamingChargingNotifications_V1.1": "charging-notifications",
}
# The Acceptable Use Policy names its limits by service.
AUP_NAMES = {
    "evse-data-pull": "PullEVSEData",
    "evse-status-pull": "PullEVSEStatus",
    "remote-start": "AuthorizeRemoteStart",
    "remote-stop": "AuthorizeRemoteStop",
    "cdr-pull": "GetChargeDetailRecords",
    "evse-pricing-pull": "PullEVSEPricing",
    "pricing-products-pull": "PullPricingProductData",
    "charging-notifications": "ChargingNotifications",
}
# The contract requires SessionID on remote start, yet Hubject assigns it (profile quirk
# remote-start-no-sessionid; spec 02_EMP_Services_and_Operations:224). A client that follows the prose must pass.
IGNORE_REQUIRED = {"eRoamingAuthorizeRemoteStart_v2.1": ("SessionID",)}
MAX_PAGE_SIZE = 2000


@dataclass(frozen=True)
class Op:
    key: str
    operation_id: str
    path: str
    role: str  # "served" (EMP calls Hubject) or "driven" (Hubject calls the EMP)
    request_schema: Any
    response_schema: Any

    @property
    def names(self) -> set[str]:
        return {self.key, self.operation_id}


def load_ops(doc: dict) -> list[Op]:
    ops = []
    for path, item in doc["paths"].items():
        o = item["post"]
        oid = o["operationId"]
        if oid not in KEYS:
            raise KeyError(f"operation {oid} is new in the pinned contract; name it in oicp.KEYS")
        direction = o.get("x-emp-direction") or ("SEND" if "To `SEND`" in (o.get("description") or "") else "RECEIVE")
        ops.append(
            Op(
                KEYS[oid],
                oid,
                path,
                "served" if direction == "SEND" else "driven",
                o["requestBody"]["content"]["application/json"]["schema"],
                o["responses"]["200"]["content"]["application/json"]["schema"],
            )
        )
    return ops


class Oicp:
    def __init__(
        self,
        mock: Mock,
        world: World,
        *,
        enforce_aup: bool = False,
        limits: dict[str, int] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.mock, self.world = mock, world
        self.doc = contracts.source(EMP_FILE)
        self.ops = load_ops(self.doc)
        self.codes = oicp_status_codes(self.doc)
        self.enforce_aup = enforce_aup
        values = contracts.profile(PROFILE_FILE)["rate_limits_per_minute"]["values"]
        self.limits = {**values, **(limits or {})}
        self.clock = clock
        self._windows: dict[str, tuple[int, int]] = {}  # AUP name -> (minute, count)
        self._validators = {
            o.key: (
                schema.validator(self.doc, o.request_schema, IGNORE_REQUIRED.get(o.operation_id, ())),
                schema.validator(self.doc, o.response_schema),
            )
            for o in self.ops
        }

    # ---- helpers -------------------------------------------------------------------------------------------
    def served(self) -> list[Op]:
        return [o for o in self.ops if o.role == "served"]

    def driven(self) -> list[Op]:
        return [o for o in self.ops if o.role == "driven"]

    def by_key(self, key: str) -> Op | None:
        return next((o for o in self.ops if key in o.names), None)

    def status(self, code: str, description: str | None = None, info: str | None = None) -> dict:
        s: dict[str, Any] = {"Code": code, "Description": (description or self.codes.get(code, ""))[:200]}
        if info:
            s["AdditionalInfo"] = info[:1000]
        return s

    def ok_body(self, op: Op, **fields: Any) -> dict:
        body = schema.sample(self.doc, op.response_schema, prefer="StatusCode")
        if "Result" in self._props(op):
            body["Result"] = True
        if "AuthorizationStatus" in self._props(op):
            body["AuthorizationStatus"] = "Authorized"
        if "StatusCode" in self._props(op):
            body["StatusCode"] = success_status(self.codes)
        body.update(fields)
        return body

    def failure_body(self, op: Op, code: str, description: str | None = None, info: str | None = None) -> dict:
        body = schema.sample(self.doc, op.response_schema, prefer="StatusCode")
        if "Result" in self._props(op):
            body["Result"] = False
        if "AuthorizationStatus" in self._props(op):
            body["AuthorizationStatus"] = "NotAuthorized"
        body["StatusCode"] = self.status(code, description, info)
        return body

    def _props(self, op: Op) -> set[str]:
        node = schema.resolve(self.doc, op.response_schema)
        if "oneOf" in node:
            node = next(
                (
                    schema.resolve(self.doc, b)
                    for b in node["oneOf"]
                    if "StatusCode" in schema.resolve(self.doc, b).get("properties", {})
                ),
                schema.resolve(self.doc, node["oneOf"][0]),
            )
        return set(node.get("properties", {}))

    def over_limit(self, op: Op) -> int | None:
        """Seconds to wait if the Acceptable Use Policy limit for this service is spent, else None."""
        name = AUP_NAMES.get(op.key)
        if not (self.enforce_aup and name in self.limits):
            return None
        now = self.clock()
        minute = int(now // 60)
        window, count = self._windows.get(name, (minute, 0))
        if window != minute:
            window, count = minute, 0
        count += 1
        self._windows[name] = (window, count)
        return int(60 - (now % 60)) + 1 if count > self.limits[name] else None

    def reset(self) -> None:
        self._windows.clear()

    # ---- the request pipeline ---------------------------------------------------------------------------------
    def handler(self, op: Op):
        async def handle(request: Request) -> Response:
            raw = await request.body()
            path_params = dict(request.path_params)
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                entry = self.mock.record(request, op.key, path_params, raw.decode(errors="replace"), None)
                return self.mock.finish(
                    entry, JSONResponse(self.failure_body(op, "022", info="body is not JSON"), status_code=400)
                )
            rule = self.mock.rules.take(op.names, body, path_params)
            entry = self.mock.record(request, op.key, path_params, body, rule)
            early = await self.mock.pre_response(rule)
            if early is not None:
                return self.mock.finish(entry, early)
            if rule and "status_code" in rule.do:
                d = rule.do
                return self.mock.finish(
                    entry,
                    JSONResponse(
                        self.failure_body(op, d["status_code"], d.get("description"), d.get("additional_info"))
                    ),
                )
            wait = self.over_limit(op)
            if wait is not None:
                return self.mock.finish(
                    entry,
                    JSONResponse({"message": "Too Many Requests"}, status_code=429, headers={"Retry-After": str(wait)}),
                )
            problems = schema.errors(self._validators[op.key][0], body)
            if problems:
                return self.mock.finish(
                    entry, JSONResponse(self.failure_body(op, "022", info="; ".join(problems[:5])), status_code=400)
                )
            return self.mock.finish(entry, JSONResponse(self.behave(op, body, request)))

        return handle

    # ---- default behaviour of the served operations ------------------------------------------------------------
    def behave(self, op: Op, body: dict, request: Request) -> dict:
        fn = getattr(self, "do_" + op.key.replace("-", "_"), None)
        return fn(op, body, request) if fn else self.ok_body(op)

    def do_remote_start(self, op: Op, body: dict, _: Request) -> dict:
        evse = self.world.evses.get(body["EvseID"])
        if evse is None:
            return self.failure_body(op, "603")
        if not evse.compatible:
            return self.failure_body(op, "604")
        if evse.out_of_service:
            return self.failure_body(op, "700")
        if evse.session_id:
            return self.failure_body(op, "602")
        if evse.reserved_by and evse.reserved_by != body["ProviderID"]:
            return self.failure_body(op, "601")
        session = self.world.start(evse, body["ProviderID"], body["Identification"])
        return self.ok_body(op, SessionID=session.id)

    def do_remote_stop(self, op: Op, body: dict, _: Request) -> dict:
        session = self.world.sessions.get(body["SessionID"])
        if session is None or session.stopped is not None:
            return self.failure_body(op, "400")
        self.world.stop(session)
        return self.ok_body(op, SessionID=session.id)

    def do_reservation_start(self, op: Op, body: dict, _: Request) -> dict:
        evse = self.world.evses.get(body["EvseID"])
        if evse is None:
            return self.failure_body(op, "603")
        if evse.session_id or evse.reserved_by:
            return self.failure_body(op, "601")
        sid = self.world.new_session_id()
        evse.reserved_by = body["ProviderID"]
        self.world.reservations[sid] = evse.id
        return self.ok_body(op, SessionID=sid)

    def do_cdr_pull(self, op: Op, body: dict, request: Request) -> dict:
        wanted = body.get("SessionID")
        stopped = [s for s in self.world.sessions.values() if s.stopped and (wanted is None or s.id == wanted)]
        stopped = [s for s in stopped if body["From"] <= iso(s.stopped) <= body["To"]]
        item_schema = schema.resolve(self.doc, op.response_schema)["properties"]["content"]["items"]
        rows = []
        for s in stopped:
            row = schema.sample(self.doc, item_schema)
            row.update(self.world.cdr_fields(s))
            rows.append(row)
        return self.page(op, rows, request)

    def do_evse_data_pull(self, op: Op, body: dict, request: Request) -> dict:
        item_schema = schema.resolve(self.doc, op.response_schema)["properties"]["content"]["items"]
        rows = []
        for e in self.world.evses.values():
            row = schema.sample(self.doc, item_schema)
            row.update({"EvseID": e.id, "OperatorID": e.operator_id, "IsHubjectCompatible": e.compatible})
            rows.append(row)
        return self.page(op, rows, request)

    def do_evse_status_pull(self, op: Op, body: dict, request: Request) -> dict:
        node = schema.resolve(self.doc, op.response_schema)
        variant = next(
            b for b in node.get("oneOf", [node]) if "EvseStatuses" in schema.resolve(self.doc, b).get("properties", {})
        )
        record_schema = schema.resolve(self.doc, variant)["properties"]["EvseStatuses"]["properties"][
            "OperatorEvseStatus"
        ]["items"]["properties"]["EvseStatusRecord"]["items"]
        by_operator: dict[str, list] = {}
        for e in self.world.evses.values():
            rec = schema.sample(self.doc, record_schema)
            rec.update({"EvseID": e.id, "EvseStatus": e.status})
            by_operator.setdefault(e.operator_id, []).append(rec)
        return {
            "EvseStatuses": {
                "OperatorEvseStatus": [
                    {"OperatorID": op_id, "EvseStatusRecord": recs} for op_id, recs in by_operator.items()
                ]
            }
        }

    def page(self, op: Op, rows: list, request: Request) -> dict:
        """Spring-style page used by the paged pulls; ?page= and ?size= as in the profile (size at most 2000)."""
        size = min(max(int(request.query_params.get("size", 20)), 1), MAX_PAGE_SIZE)
        number = max(int(request.query_params.get("page", 0)), 0)
        chunk = rows[number * size : (number + 1) * size]
        total_pages = max(-(-len(rows) // size), 1)
        body = self.ok_body(op)
        body.update(
            {
                "content": chunk,
                "number": number,
                "size": size,
                "totalElements": len(rows),
                "totalPages": total_pages,
                "first": number == 0,
                "last": number >= total_pages - 1,
                "numberOfElements": len(chunk),
            }
        )
        return body

    # ---- hub-side catalogue used by the admin API -----------------------------------------------------------------
    def set_evse(self, evse_id: str, spec: dict) -> Evse:
        evse = self.world.evses.get(evse_id) or Evse(evse_id, spec.get("operator_id", "TR*ZES"))
        self.world.evses[evse_id] = evse
        for field in ("compatible", "out_of_service"):
            if field in spec:
                setattr(evse, field, bool(spec[field]))
        return evse
