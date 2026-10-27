"""The GIREVE mock as one ASGI app: the IOP (OCPI 2.2.1), PNCP webhooks and the /_mock admin API."""

from __future__ import annotations

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import contracts
from ..mockcore import Mock, admin_routes
from . import driver, pncp
from .hub import PROFILE_FILE, VERSION, Hub


def create_app(
    *,
    base_url: str = "http://testserver",
    auto_callbacks: bool = True,
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    mock = Mock("gireve")
    client = http_client or httpx.AsyncClient(timeout=15)
    hub = Hub(mock, base_url=base_url, client=client, auto_callbacks=auto_callbacks)
    v = f"/ocpi/{VERSION}"

    served = [
        # (operation key, path, methods, module call?, behaviour)
        ("versions", "/ocpi/versions", ["GET"], False, hub.versions),
        ("version-details", v, ["GET"], False, hub.version_details),
        ("credentials-get", f"{v}/credentials", ["GET"], False, hub.credentials),
        ("credentials-write", f"{v}/credentials", ["POST", "PUT", "DELETE"], False, hub.credentials),
        ("locations-get", f"{v}/locations", ["GET"], True, hub.locations_get),
        ("tariffs-get", f"{v}/tariffs", ["GET"], True, hub.tariffs_get),
        ("sessions-get", f"{v}/sessions", ["GET"], True, hub.not_implemented),
        ("cdrs-get", f"{v}/cdrs", ["GET"], True, hub.not_implemented),
        ("token-put", f"{v}/tokens/{{country_code}}/{{party_id}}/{{token_uid}}", ["PUT"], True, hub.token_put),
        (
            "token-other",
            f"{v}/tokens/{{country_code}}/{{party_id}}/{{token_uid}}",
            ["GET", "PATCH"],
            True,
            hub.not_implemented,
        ),
        ("command", f"{v}/commands/{{command}}", ["POST"], True, hub.command),
    ]

    def known() -> set[str]:
        return {key for key, *_ in served} | set(driver.DRIVEN) | {"pncp-webhook"}

    def describe() -> dict:
        return {
            "profile": {PROFILE_FILE: contracts.profile_sha256(PROFILE_FILE)},
            "ocpi_version": VERSION,
            "served": sorted(key for key, *_ in served),
            "driven": list(driver.DRIVEN),
            "registered": hub.reg.registered,
            "auto_callbacks": hub.auto_callbacks,
            "pncp_events": sorted(pncp.event_types()),
        }

    async def drive(request: Request) -> Response:
        key = request.path_params["key"]
        spec = await request.json() if await request.body() else {}
        try:
            if key == "register":
                if not spec.get("versions_url") or not spec.get("temp_token"):
                    return JSONResponse({"error": "`versions_url` and `temp_token` are required"}, status_code=422)
                out = await driver.register(hub, versions_url=spec["versions_url"], temp_token=spec["temp_token"])
            elif key == "unregister":
                out = await driver.unregister(hub)
            elif key in driver.DRIVEN:
                out = await driver.call(hub, key, spec)
            else:
                return JSONResponse(
                    {"error": f"unknown operation {key!r}", "known": list(driver.DRIVEN)}, status_code=404
                )
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"call to the eMSP failed: {e}"}, status_code=502)
        return JSONResponse(out)

    async def commands(_: Request) -> Response:
        return JSONResponse(list(hub.commands.values()))

    async def deliver(request: Request) -> Response:
        uid = request.path_params["uid"]
        if uid not in hub.commands:
            return JSONResponse({"error": "no such command"}, status_code=404)
        spec = await request.json() if await request.body() else {}
        return JSONResponse(await hub.deliver_result(uid, spec.get("result")))

    async def tokens(_: Request) -> Response:
        return JSONResponse([t for t in hub.tokens.values()])

    async def sessions(_: Request) -> Response:
        return JSONResponse(list(hub.sessions.values()))

    async def pncp_emit(request: Request) -> Response:
        spec = await request.json()
        missing = [f for f in ("target", "secret", "event_type") if not spec.get(f)]
        if missing:
            return JSONResponse({"error": "missing: " + ", ".join(missing)}, status_code=422)
        if spec["event_type"] not in pncp.event_types():
            return JSONResponse({"error": "unknown event_type", "known": sorted(pncp.event_types())}, status_code=422)
        try:
            out = await pncp.emit(
                client,
                target=spec["target"],
                secret=spec["secret"],
                event_type=spec["event_type"],
                data=spec.get("data"),
                corrupt_signature=bool(spec.get("corrupt_signature")),
            )
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"delivery failed: {e}"}, status_code=502)
        return JSONResponse(out)

    extra = [
        Route("/_mock/drive/{key}", drive, methods=["POST"]),
        Route("/_mock/commands", commands, methods=["GET"]),
        Route("/_mock/commands/{uid}/deliver", deliver, methods=["POST"]),
        Route("/_mock/tokens", tokens, methods=["GET"]),
        Route("/_mock/sessions", sessions, methods=["GET"]),
        Route("/_mock/pncp/emit", pncp_emit, methods=["POST"]),
    ]
    routes = admin_routes(mock, known_operations=known, describe=describe, reset=hub.reset, extra=extra)
    routes += [
        Route(path, hub.serve(key, module=module, fn=fn), methods=methods) for key, path, methods, module, fn in served
    ]
    app = Starlette(routes=routes)
    app.state.mock, app.state.hub = mock, hub
    return app
