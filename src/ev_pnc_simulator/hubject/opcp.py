"""OPCP 1.0 (Plug&Charge) side of the Hubject mock, from the pinned eMSP-role contracts.

Unlike OICP, OPCP uses real HTTP codes and an error body `{"errorMessages": [...], "traceId": "..."}`.
Every status code a contract declares can be injected. The webhook endpoints are stateful (see webhooks.py);
the other operations answer with a minimal valid sample of their first 2xx response.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import contracts, schema
from ..mockcore import Mock
from .webhooks import Webhooks

FILES = {
    "ccp": "hubject-opcp-ccp.mo.openapi.json",
    "cps": "hubject-opcp-cps.mo.openapi.json",
    "est": "hubject-opcp-est.mo.openapi.json",
    "pcp": "hubject-opcp-pcp.mo.openapi.json",
    "rcp": "hubject-opcp-rcp.mo.openapi.json",
    "webhooks": "hubject-opcp-webhooks.mo.openapi.json",
}
METHODS = ("get", "post", "put", "delete")


@dataclass(frozen=True)
class OpcpOp:
    service: str
    operation_id: str
    method: str
    path: str
    responses: dict[str, Any]
    doc_name: str

    @property
    def names(self) -> set[str]:
        return {self.operation_id, f"{self.service}:{self.operation_id}"}

    def declared_codes(self) -> list[str]:
        return list(self.responses)


def load_ops() -> list[OpcpOp]:
    ops = []
    for service, fname in FILES.items():
        doc = contracts.source(fname)
        for path, item in doc["paths"].items():
            for method in METHODS:
                if method in item:
                    o = item[method]
                    ops.append(OpcpOp(service, o["operationId"], method.upper(), path, o["responses"], fname))
    return ops


def error_body(*messages: str) -> dict:
    return {"errorMessages": list(messages), "traceId": str(uuid.uuid4())}


class Opcp:
    def __init__(self, mock: Mock, *, require_auth: bool = True) -> None:
        self.mock, self.require_auth = mock, require_auth
        self.ops = load_ops()
        self.webhooks = Webhooks()
        self._docs = {fname: contracts.source(fname) for fname in FILES.values()}

    def names(self) -> set[str]:
        return set().union(*(o.names for o in self.ops))

    def by_name(self, name: str) -> OpcpOp | None:
        return next((o for o in self.ops if name in o.names), None)

    def routes(self) -> list[Route]:
        routes = []
        for op in self.ops:
            template = re.sub(r"\{([^}]+)\}", lambda m: "{" + m.group(1).replace("-", "_") + "}", op.path)
            for path in {template, template.rstrip("/")}:
                routes.append(Route(path, self.handler(op), methods=[op.method]))
        # the EST cacerts path starts with a parameter; keep it behind the literal ones
        routes.sort(key=lambda r: r.path.startswith("/{"))
        return routes

    def handler(self, op: OpcpOp):
        async def handle(request: Request) -> Response:
            raw = await request.body()
            try:
                body: Any = (
                    json.loads(raw)
                    if raw and "json" in request.headers.get("content-type", "")
                    else (raw.decode(errors="replace") if raw else None)
                )
            except ValueError:
                body = raw.decode(errors="replace")
            params = dict(request.path_params)
            rule = self.mock.rules.take(op.names, body if isinstance(body, dict) else {}, params)
            entry = self.mock.record(request, op.operation_id, params, body, rule)
            if rule and "http_status" in rule.do and "body" not in rule.do and op.service != "est":
                rule.do["body"] = error_body(f"injected {rule.do['http_status']}")
            early = await self.mock.pre_response(rule)
            if early is not None:
                return self.mock.finish(entry, early)
            if rule and "status_code" in rule.do:
                return self.mock.finish(
                    entry,
                    JSONResponse(
                        error_body("status_code is an OICP concept; use http_status for OPCP"), status_code=422
                    ),
                )
            if self.require_auth and not request.headers.get("authorization", "").lower().startswith("bearer "):
                return self.mock.finish(entry, JSONResponse(error_body("missing bearer token"), status_code=401))
            if op.service == "webhooks":
                return self.mock.finish(entry, self.webhook_api(op, params, body))
            return self.mock.finish(entry, self.default(op))

        return handle

    def default(self, op: OpcpOp) -> Response:
        code = min((c for c in op.responses if c.startswith("2")), key=int)
        resp = op.responses[code]
        content = resp.get("content") or {}
        doc = self._docs[op.doc_name]
        if "application/json" in content:
            node = content["application/json"].get("schema", {})
            try:
                return JSONResponse(schema.sample(doc, node), status_code=int(code))
            except schema.SampleError:
                return JSONResponse({}, status_code=int(code))
        if content:
            media = next(iter(content))
            return Response(
                base64.b64encode(b"mock-" + op.operation_id.encode()), status_code=int(code), media_type=media
            )
        return Response(status_code=int(code))

    def webhook_api(self, op: OpcpOp, params: dict, body: Any) -> Response:
        w = self.webhooks
        if op.operation_id == "create-webhook-endpoint":
            status, out = w.create(body if isinstance(body, dict) else {})
        elif op.operation_id == "get-webhook-endpoints":
            status, out = 200, [{k: v for k, v in e.items() if k != "secret"} for e in w.endpoints.values()]
        elif op.operation_id == "get-webhook-endpoint":
            e = w.endpoints.get(params["id"])
            status, out = (
                (200, {k: v for k, v in e.items() if k != "secret"}) if e else (404, error_body("no such endpoint"))
            )
        elif op.operation_id == "update-webhook-endpoint":
            status, out = w.update(params["id"], body if isinstance(body, dict) else {})
            if status == 200:
                out = {k: v for k, v in out.items() if k != "secret"}
        else:  # delete
            if params["id"] in w.endpoints:
                del w.endpoints[params["id"]]
                return Response(status_code=200)
            status, out = 404, error_body("no such endpoint")
        return JSONResponse(out, status_code=status)
