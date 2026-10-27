"""OCPI 2.2.1 plumbing shared by the OCPI mocks: envelope, token header, routing and tracing headers, paging."""

from __future__ import annotations

import base64
import binascii
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

OK = 1000
GENERIC_CLIENT_ERROR = 2000
INVALID_PARAMETERS = 2001
UNKNOWN_LOCATION = 2003
UNKNOWN_TOKEN = 2004
GENERIC_SERVER_ERROR = 3000

ROUTING = ("ocpi-to-country-code", "ocpi-to-party-id", "ocpi-from-country-code", "ocpi-from-party-id")
TRACING = ("x-request-id", "x-correlation-id")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def envelope(data: Any = None, status_code: int = OK, message: str | None = None) -> dict:
    body: dict[str, Any] = {"status_code": status_code, "timestamp": now_iso()}
    if data is not None:
        body["data"] = data
    if message:
        body["status_message"] = message
    return body


def reply(
    request: Request | None,
    data: Any = None,
    *,
    status_code: int = OK,
    message: str | None = None,
    http: int = 200,
    headers: dict | None = None,
) -> JSONResponse:
    """An OCPI response; tracing ids from the request are echoed (the GIREVE profile requires it)."""
    out = dict(headers or {})
    if request is not None:
        for name in ("x-request-id", "x-correlation-id"):
            if name in request.headers:
                out[name] = request.headers[name]
    return JSONResponse(envelope(data, status_code, message), status_code=http, headers=out)


def encode_token(token: str) -> str:
    """OCPI 2.2.1 header value `Token <base64 of the token>`; GIREVE encodes every token."""
    return "Token " + base64.b64encode(token.encode()).decode()


def decode_token(header: str | None) -> str | None:
    """The token inside an `Authorization: Token <base64>` header, or None if absent or not base64."""
    if not header or not header.startswith("Token "):
        return None
    try:
        return base64.b64decode(header[6:], validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None


def missing_headers(request: Request, names: tuple[str, ...]) -> list[str]:
    return [n for n in names if n not in request.headers]


def paginate(request: Request, rows: list[dict], *, max_limit: int) -> tuple[list[dict], dict[str, str]]:
    """OCPI paging: offset and limit query parameters, Link, X-Total-Count and X-Limit headers."""
    offset = max(int(request.query_params.get("offset", 0)), 0)
    limit = min(max(int(request.query_params.get("limit", max_limit)), 1), max_limit)
    chunk = rows[offset : offset + limit]
    headers = {"X-Total-Count": str(len(rows)), "X-Limit": str(limit)}
    if offset + limit < len(rows):
        query = dict(request.query_params)
        query.update({"offset": offset + limit, "limit": limit})
        headers["Link"] = f'<{request.url.replace(query=urlencode(query))}>; rel="next"'
    return chunk, headers


def window(request: Request, rows: list[dict], *, max_range: timedelta | None) -> tuple[list[dict], str | None]:
    """Filter by date_from and date_to on last_updated. Returns (rows, error message)."""
    dfrom, dto = request.query_params.get("date_from"), request.query_params.get("date_to")
    try:
        start = parse_time(dfrom) if dfrom else None
        end = parse_time(dto) if dto else None
    except ValueError:
        return [], "date_from and date_to must be ISO 8601 timestamps"
    if max_range is not None and start is not None and (end or datetime.now(UTC)) - start > max_range:
        return [], "the requested date range is longer than this server allows"
    keep = [
        r
        for r in rows
        if (start is None or parse_time(r["last_updated"]) >= start)
        and (end is None or parse_time(r["last_updated"]) < end)
    ]
    return keep, None


def range_from_text(text: str) -> timedelta:
    """'1 month' / '30 days' from a profile as a timedelta (a month is 31 days, to be lenient)."""
    m = re.fullmatch(r"\s*(\d+)\s*(day|week|month)s?\s*", text)
    if not m:
        raise ValueError(f"cannot read a duration from {text!r}")
    return timedelta(days=int(m[1]) * {"day": 1, "week": 7, "month": 31}[m[2]])


def error_for(request: Request, status_code: int, message: str, http: int = 200) -> Response:
    return reply(request, None, status_code=status_code, message=message, http=http)
