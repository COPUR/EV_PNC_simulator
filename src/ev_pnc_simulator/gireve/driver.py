"""The IOP calling the eMSP under test: registration, real-time authorize and the pushes.

Each call reports what was sent, what came back and whether it was acceptable: HTTP 2xx, an OCPI envelope with
status_code 1000, and (for module calls) the tracing ids echoed, which the profile requires.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from .. import ocpi
from . import fixtures
from .hub import VERSION, Hub

DRIVEN = (
    "register",
    "unregister",
    "authorize",
    "location-put",
    "tariff-put",
    "session-put",
    "session-patch",
    "cdr-post",
)
ALLOWED = {"ALLOWED", "BLOCKED", "EXPIRED", "NO_CREDIT", "NOT_ALLOWED"}


def _step(name: str, resp: httpx.Response | None, ok: bool, note: str = "") -> dict:
    return {"step": name, "status": resp.status_code if resp is not None else None, "ok": ok, "note": note}


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _good(resp: httpx.Response, body: Any) -> bool:
    return resp.status_code in (200, 201) and isinstance(body, dict) and body.get("status_code") == ocpi.OK


def module_headers(hub: Hub, token: str) -> dict[str, str]:
    return {
        "Authorization": ocpi.encode_token(token),
        "X-Request-ID": str(uuid.uuid4()),
        "X-Correlation-ID": str(uuid.uuid4()),
        "OCPI-from-country-code": fixtures.CPO_COUNTRY,
        "OCPI-from-party-id": fixtures.CPO_PARTY,
        "OCPI-to-country-code": hub.reg.emsp_party[0],
        "OCPI-to-party-id": hub.reg.emsp_party[1],
    }


async def register(hub: Hub, *, versions_url: str, temp_token: str) -> dict:
    """Register-FromIOP: the IOP reads the eMSP's versions and endpoints, then POSTs its own credentials."""
    steps: list[dict] = []
    auth = {
        "Authorization": ocpi.encode_token(temp_token),
        "X-Request-ID": str(uuid.uuid4()),
        "X-Correlation-ID": str(uuid.uuid4()),
    }
    client = hub.client

    resp = await client.get(versions_url, headers=auth)
    body = _json(resp)
    offered = [v.get("version") for v in (body or {}).get("data") or []] if _good(resp, body) else []
    steps.append(_step("GET versions", resp, VERSION in offered, f"offered {offered}"))
    if VERSION not in offered:
        return {"registered": False, "steps": steps}
    details_url = next(v["url"] for v in body["data"] if v["version"] == VERSION)

    resp = await client.get(details_url, headers=auth)
    body = _json(resp)
    endpoints = (
        {e["identifier"]: e["url"] for e in (body or {}).get("data", {}).get("endpoints", [])}
        if _good(resp, body)
        else {}
    )
    steps.append(_step("GET version details", resp, "credentials" in endpoints, f"endpoints {sorted(endpoints)}"))
    if "credentials" not in endpoints:
        return {"registered": False, "steps": steps}

    iop_token = Hub.new_token()
    mine = {"token": iop_token, "url": hub.url("/ocpi/versions"), "roles": [hub.hub_role()]}
    resp = await client.post(endpoints["credentials"], json=mine, headers=auth)
    body = _json(resp)
    data = (body or {}).get("data") or {}
    roles = data.get("roles") or []
    ok = _good(resp, body) and bool(data.get("token")) and bool(roles)
    steps.append(
        _step("POST credentials", resp, ok, "answer must carry the eMSP's final token and roles" if not ok else "")
    )
    if not ok:
        return {"registered": False, "steps": steps}

    hub.reg.registered = True
    hub.reg.iop_token = iop_token
    hub.reg.emsp_token = data["token"]
    hub.reg.emsp_endpoints = endpoints
    hub.reg.emsp_party = (
        roles[0].get("country_code", fixtures.EMSP_COUNTRY),
        roles[0].get("party_id", fixtures.EMSP_PARTY),
    )
    return {"registered": True, "steps": steps, "emsp_party": list(hub.reg.emsp_party), "emsp_endpoints": endpoints}


async def unregister(hub: Hub) -> dict:
    """Unregister-FromIOP: DELETE on the eMSP's credentials endpoint with the token the eMSP issued."""
    if not hub.reg.registered:
        return {"registered": False, "steps": [], "note": "not registered"}
    url = hub.reg.emsp_endpoints["credentials"]
    resp = await hub.client.delete(url, headers=module_headers(hub, hub.reg.emsp_token or ""))
    body = _json(resp)
    ok = _good(resp, body)
    if ok:
        hub.reg.registered, hub.reg.iop_token, hub.reg.emsp_token = False, None, None
    return {"registered": not ok, "steps": [_step("DELETE credentials", resp, ok)]}


def _endpoint(hub: Hub, name: str) -> str:
    try:
        return hub.reg.emsp_endpoints[name]
    except KeyError as e:
        raise LookupError(f"the eMSP did not advertise a {name!r} endpoint (or the mock is not registered)") from e


async def call(hub: Hub, key: str, spec: dict) -> dict:
    """Run one driven operation (everything in DRIVEN except register and unregister)."""
    if not hub.reg.registered:
        raise LookupError("register first: POST /_mock/drive/register")
    cc, party = fixtures.CPO_COUNTRY, fixtures.CPO_PARTY
    method, params, body = "PUT", None, spec.get("body")
    if key == "authorize":
        uid = spec.get("token_uid", "04A1B2C3D4E5F6")
        url = f"{_endpoint(hub, 'tokens').rstrip('/')}/{uid}/authorize"
        method, params = "POST", {"type": spec.get("type", "RFID")}
        body = body if body is not None else {"location_id": "LOC0001", "evse_uids": ["FR*ABC*E10001"]}
    elif key == "location-put":
        loc = body if body is not None else hub.locations[0]
        url, body = f"{_endpoint(hub, 'locations').rstrip('/')}/{cc}/{party}/{loc['id']}", loc
    elif key == "tariff-put":
        tar = body if body is not None else hub.tariffs[0]
        url, body = f"{_endpoint(hub, 'tariffs').rstrip('/')}/{cc}/{party}/{tar['id']}", tar
    elif key in ("session-put", "session-patch"):
        sid = spec.get("session_id", "SES0001")
        base = f"{_endpoint(hub, 'sessions').rstrip('/')}/{cc}/{party}/{sid}"
        url = base
        if key == "session-put":
            body = body if body is not None else fixtures.session(sid)
        else:
            method, body = (
                "PATCH",
                body if body is not None else {"kwh": 15.0, "status": "COMPLETED", "last_updated": ocpi.now_iso()},
            )
    elif key == "cdr-post":
        method, url = "POST", _endpoint(hub, "cdrs")
        body = (
            body
            if body is not None
            else fixtures.cdr(
                spec.get("cdr_id", "CDR0001"),
                spec.get("session_id", "SES0001"),
                spec.get("remark", "[*] priced by the hub"),
            )
        )
    else:
        raise KeyError(key)
    headers = module_headers(hub, hub.reg.emsp_token or "")
    resp = await hub.client.request(method, url, json=body, params=params, headers=headers)
    answer = _json(resp)
    problems = []
    if not _good(resp, answer):
        problems.append("expected HTTP 200 or 201 and an OCPI status_code 1000")
    for name in ("x-request-id", "x-correlation-id"):
        if resp.headers.get(name) != headers[name.title().replace("Id", "ID")]:
            problems.append(f"{name} was not echoed")
    if key == "authorize" and not problems:
        info = answer.get("data") or {}
        if info.get("allowed") not in ALLOWED:
            problems.append("data.allowed is not an AuthorizationInfo value")
        if body.get("location_id") and (info.get("location") or {}).get("location_id") != body["location_id"]:
            problems.append("the location reference was not echoed")
    return {
        "operation": key,
        "method": method,
        "url": str(resp.request.url),
        "sent": body,
        "status": resp.status_code,
        "response": answer,
        "ok": not problems,
        "problems": problems,
    }
