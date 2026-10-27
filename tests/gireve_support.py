"""Shared fixtures for the GIREVE mock tests: a stand-in eMSP and helpers to talk to the mock as an eMSP would."""

from __future__ import annotations

import base64

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ev_pnc_simulator.gireve.app import create_app

EMSP = "http://emsp"
HUB = "http://hub"
TEMP_TOKEN = "TEMP-TOKEN-A"
EMSP_TOKEN = "EMSP-FINAL-TOKEN"
V = "/ocpi/2.2.1"


def b64(token: str) -> str:
    return "Token " + base64.b64encode(token.encode()).decode()


class FakeEmsp:
    """What a conforming eMSP answers. `seen` keeps every call; flags let a test break one behaviour."""

    def __init__(self, *, versions=("2.2.1",), echo_ids=True, authorize_status="ALLOWED") -> None:
        self.versions, self.echo_ids, self.authorize_status = versions, echo_ids, authorize_status
        self.seen: list[dict] = []
        self.hub_credentials: dict | None = None
        routes = [
            Route("/ocpi/versions", self.get_versions, methods=["GET"]),
            Route(V, self.get_details, methods=["GET"]),
            Route(f"{V}/credentials", self.credentials, methods=["POST", "DELETE"]),
            Route(f"{V}/tokens/{{uid}}/authorize", self.authorize, methods=["POST"]),
            Route(f"{V}/locations/{{cc}}/{{party}}/{{id}}", self.accept, methods=["PUT"]),
            Route(f"{V}/tariffs/{{cc}}/{{party}}/{{id}}", self.accept, methods=["PUT"]),
            Route(f"{V}/sessions/{{cc}}/{{party}}/{{id}}", self.accept, methods=["PUT", "PATCH"]),
            Route(f"{V}/cdrs", self.accept, methods=["POST"]),
            Route("/ocpi/command-result/{uid}", self.accept, methods=["POST"]),
        ]
        self.app = Starlette(routes=routes)

    async def _note(self, request: Request) -> dict:
        raw = await request.body()
        entry = {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "headers": dict(request.headers),
            "body": httpx.Response(200, content=raw).json() if raw else None,
        }
        self.seen.append(entry)
        return entry

    def _reply(self, request: Request, data=None, status_code=1000) -> JSONResponse:
        headers = {}
        if self.echo_ids:
            headers = {n: request.headers[n] for n in ("x-request-id", "x-correlation-id") if n in request.headers}
        body = {"status_code": status_code, "timestamp": "2026-10-27T10:00:00Z"}
        if data is not None:
            body["data"] = data
        return JSONResponse(body, headers=headers)

    async def get_versions(self, request: Request):
        await self._note(request)
        return self._reply(request, [{"version": v, "url": f"{EMSP}/ocpi/{v}"} for v in self.versions])

    async def get_details(self, request: Request):
        await self._note(request)
        names = ("credentials", "tokens", "locations", "tariffs", "sessions", "cdrs")
        endpoints = [{"identifier": n, "role": "SENDER", "url": f"{EMSP}{V}/{n}"} for n in names]
        return self._reply(request, {"version": "2.2.1", "endpoints": endpoints})

    async def credentials(self, request: Request):
        entry = await self._note(request)
        if request.method == "DELETE":
            return self._reply(request)
        self.hub_credentials = entry["body"]
        roles = [{"role": "EMSP", "party_id": "TST", "country_code": "TR", "business_details": {"name": "Test eMSP"}}]
        return self._reply(request, {"token": EMSP_TOKEN, "url": f"{EMSP}/ocpi/versions", "roles": roles})

    async def authorize(self, request: Request):
        entry = await self._note(request)
        ref = entry["body"] or {}
        info = {"allowed": self.authorize_status, "location": ref}
        return self._reply(request, info)

    async def accept(self, request: Request):
        await self._note(request)
        return self._reply(request)


def mock_for(emsp: FakeEmsp, **kwargs) -> TestClient:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=emsp.app), base_url=EMSP)
    return TestClient(create_app(base_url=HUB, http_client=http, **kwargs))


def register(c: TestClient) -> dict:
    return c.post(
        "/_mock/drive/register", json={"versions_url": f"{EMSP}/ocpi/versions", "temp_token": TEMP_TOKEN}
    ).json()


def iop_token(c: TestClient) -> str:
    return c.app.state.hub.reg.iop_token


def as_emsp(c: TestClient, **extra) -> dict:
    """Headers an eMSP sends on a module call, with the token the IOP issued during registration."""
    return {
        "Authorization": b64(iop_token(c)),
        "X-Request-ID": "req-1",
        "X-Correlation-ID": "cor-1",
        "OCPI-from-country-code": "TR",
        "OCPI-from-party-id": "TST",
        "OCPI-to-country-code": "FR",
        "OCPI-to-party-id": "ABC",
        **extra,
    }


def rfid_token(uid: str = "04A1B2C3D4E5F6", **over) -> dict:
    return {
        "country_code": "TR",
        "party_id": "TST",
        "uid": uid,
        "type": "RFID",
        "contract_id": "TR-TST-C00000001-1",
        "issuer": "Test eMSP",
        "valid": True,
        "whitelist": "ALWAYS",
        "last_updated": "2026-10-27T10:00:00Z",
        **over,
    }


def push_token(c: TestClient, token: dict | None = None, headers: dict | None = None):
    t = token or rfid_token()
    return c.put(f"{V}/tokens/{t['country_code']}/{t['party_id']}/{t['uid']}", json=t, headers=headers or as_emsp(c))
