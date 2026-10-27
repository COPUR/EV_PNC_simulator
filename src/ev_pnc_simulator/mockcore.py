"""Pieces every mock shares: the request log, rule handling and the /_mock admin API."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .inject import Rule, RuleError, RuleSet

LOG_LIMIT = 1000
MASKED = ("authorization", "x-hubject-signature", "payload-signature", "x-operator-signature")


class Mock:
    def __init__(self, name: str) -> None:
        self.name = name
        self.rules = RuleSet()
        self.log: deque[dict] = deque(maxlen=LOG_LIMIT)
        self._seq = 0

    def record(self, request: Request, operation: str, path_params: dict, body: Any, rule: Rule | None) -> dict:
        self._seq += 1
        entry = {
            "n": self._seq,
            "operation": operation,
            "method": request.method,
            "path": request.url.path,
            "path_params": dict(path_params),
            "query": dict(request.query_params),
            "headers": {
                k: ("***" if k in MASKED else v)
                for k, v in request.headers.items()
                if k not in ("host", "user-agent", "accept-encoding", "connection")
            },
            "body": body,
            "rule": rule.id if rule else None,
        }
        self.log.append(entry)
        return entry

    def finish(self, entry: dict, response: Response) -> Response:
        entry["status"] = response.status_code
        try:
            entry["response"] = json.loads(response.body) if response.body else None
        except ValueError:
            entry["response"] = None
        return response

    async def pre_response(self, rule: Rule | None) -> Response | None:
        """Wait and answer for the actions every protocol shares. None means: carry on or answer by protocol."""
        if rule is None:
            return None
        do = rule.do
        if do.get("delay_ms"):
            await asyncio.sleep(do["delay_ms"] / 1000)
        if "rate_limit" in do:
            retry = int(do["rate_limit"].get("retry_after_s", 1))
            return JSONResponse({"message": "Too Many Requests"}, status_code=429, headers={"Retry-After": str(retry)})
        if "http_status" in do:
            return JSONResponse(do.get("body", {}), status_code=do["http_status"], headers=do.get("headers"))
        return None


Handler = Callable[[Request], Awaitable[Response]]


def admin_routes(
    mock: Mock,
    *,
    known_operations: Callable[[], set[str]],
    describe: Callable[[], dict],
    reset: Callable[[], None],
    extra: list[Route] | None = None,
) -> list[Route]:
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "mock": mock.name, **describe()})

    async def add_rule(request: Request) -> Response:
        try:
            spec = await request.json()
            rule = mock.rules.add(spec, known_operations())
        except (RuleError, ValueError, AttributeError) as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        return JSONResponse(rule.public(), status_code=201)

    async def list_rules(_: Request) -> Response:
        return JSONResponse([r.public() for r in mock.rules.rules])

    async def delete_rule(request: Request) -> Response:
        if mock.rules.remove(request.path_params["rule_id"]):
            return Response(status_code=204)
        return JSONResponse({"error": "no such rule"}, status_code=404)

    async def clear_rules(_: Request) -> Response:
        mock.rules.clear()
        return Response(status_code=204)

    async def requests(request: Request) -> Response:
        op = request.query_params.get("operation")
        rows = [e for e in mock.log if op is None or e["operation"] == op]
        return JSONResponse(rows)

    async def clear_requests(_: Request) -> Response:
        mock.log.clear()
        return Response(status_code=204)

    async def do_reset(_: Request) -> Response:
        mock.rules.clear()
        mock.log.clear()
        reset()
        return Response(status_code=204)

    return [
        Route("/_mock/health", health),
        Route("/_mock/rules", add_rule, methods=["POST"]),
        Route("/_mock/rules", list_rules, methods=["GET"]),
        Route("/_mock/rules", clear_rules, methods=["DELETE"]),
        Route("/_mock/rules/{rule_id}", delete_rule, methods=["DELETE"]),
        Route("/_mock/requests", requests, methods=["GET"]),
        Route("/_mock/requests", clear_requests, methods=["DELETE"]),
        Route("/_mock/reset", do_reset, methods=["POST"]),
        *(extra or []),
    ]
