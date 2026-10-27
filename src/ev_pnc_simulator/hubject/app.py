"""The Hubject mock as one ASGI app: OICP 2.3 (EMP), OPCP 1.0 and the /_mock admin API."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import contracts
from ..mockcore import Mock, admin_routes
from . import opcp as opcp_mod
from .driver import drive
from .oicp import EMP_FILE, Oicp
from .state import World
from .webhooks import SIGNATURE_HEADERS, event_types


def create_app(
    *,
    enforce_aup: bool = False,
    require_auth: bool = True,
    http_client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] | None = None,
    time_source: Callable[[], float] = time.time,
) -> Starlette:
    mock = Mock("hubject")
    world = World(clock=clock or (lambda: datetime.now(UTC)))
    oicp = Oicp(mock, world, enforce_aup=enforce_aup, clock=time_source)
    opcp = opcp_mod.Opcp(mock, require_auth=require_auth)
    client = http_client or httpx.AsyncClient(timeout=15)

    def known() -> set[str]:
        return oicp_names | opcp.names()

    oicp_names = set().union(*(o.names for o in oicp.ops))

    def describe() -> dict:
        return {
            "contracts": {
                EMP_FILE: contracts.sha256(EMP_FILE),
                **{f: contracts.sha256(f) for f in opcp_mod.FILES.values()},
            },
            "oicp": {"served": [o.key for o in oicp.served()], "driven": [o.key for o in oicp.driven()]},
            "opcp": sorted({o.operation_id for o in opcp.ops}),
            "aup_enforced": enforce_aup,
            "auth_required": require_auth,
        }

    def reset() -> None:
        world.reset()
        oicp.reset()
        opcp.webhooks.reset()

    async def status_codes(_: Request) -> Response:
        return JSONResponse(oicp.codes)

    async def put_evse(request: Request) -> Response:
        evse = oicp.set_evse(request.path_params["evse_id"], await request.json())
        return JSONResponse(
            {
                "id": evse.id,
                "operator_id": evse.operator_id,
                "compatible": evse.compatible,
                "out_of_service": evse.out_of_service,
                "status": evse.status,
            }
        )

    async def get_evses(_: Request) -> Response:
        return JSONResponse(
            [
                {
                    "id": e.id,
                    "operator_id": e.operator_id,
                    "compatible": e.compatible,
                    "out_of_service": e.out_of_service,
                    "status": e.status,
                }
                for e in world.evses.values()
            ]
        )

    async def sessions(_: Request) -> Response:
        return JSONResponse(
            [
                {"id": s.id, "evse_id": s.evse_id, "provider_id": s.provider_id, "stopped": s.stopped is not None}
                for s in world.sessions.values()
            ]
        )

    async def do_drive(request: Request) -> Response:
        spec = await request.json()
        key = request.path_params["key"]
        if not spec.get("target"):
            return JSONResponse({"error": "`target` (base URL of the eMSP) is required"}, status_code=422)
        op = oicp.by_key(key)
        if op is None or op.role != "driven":
            return JSONResponse(
                {"error": f"{key!r} is not an operation the hub sends; see /_mock/health oicp.driven"}, status_code=404
            )
        try:
            out = await drive(
                oicp,
                client,
                key,
                target=spec["target"],
                body=spec.get("body"),
                path_params=spec.get("path_params"),
                headers=spec.get("headers"),
            )
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"call to target failed: {e}"}, status_code=502)
        return JSONResponse(out)

    async def emit(request: Request) -> Response:
        spec = await request.json()
        if spec.get("event_type") not in event_types():
            return JSONResponse({"error": "unknown event_type", "known": sorted(event_types())}, status_code=422)
        header = spec.get("signature_header", SIGNATURE_HEADERS[0])
        try:
            out = await opcp.webhooks.emit(
                client,
                endpoint_id=request.path_params["endpoint_id"],
                event_type=spec["event_type"],
                payload=spec.get("payload", {}),
                header=header,
                corrupt_signature=bool(spec.get("corrupt_signature")),
            )
        except KeyError:
            return JSONResponse({"error": "no such endpoint"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"delivery failed: {e}"}, status_code=502)
        return JSONResponse(out)

    async def secret(request: Request) -> Response:
        ep = opcp.webhooks.endpoints.get(request.path_params["endpoint_id"])
        return (
            JSONResponse({"secret": ep["secret"]})
            if ep
            else JSONResponse({"error": "no such endpoint"}, status_code=404)
        )

    extra = [
        Route("/_mock/status-codes", status_codes),
        Route("/_mock/evses", get_evses, methods=["GET"]),
        Route("/_mock/evses/{evse_id}", put_evse, methods=["PUT"]),
        Route("/_mock/sessions", sessions),
        Route("/_mock/drive/{key}", do_drive, methods=["POST"]),
        Route("/_mock/webhooks/{endpoint_id}/emit", emit, methods=["POST"]),
        Route("/_mock/webhooks/{endpoint_id}/secret", secret, methods=["GET"]),
    ]
    routes = admin_routes(mock, known_operations=known, describe=describe, reset=reset, extra=extra)
    routes += [Route(o.path, oicp.handler(o), methods=["POST"]) for o in oicp.served()]
    routes += opcp.routes()
    app = Starlette(routes=routes)
    app.state.mock, app.state.world, app.state.oicp, app.state.opcp = mock, world, oicp, opcp
    return app
