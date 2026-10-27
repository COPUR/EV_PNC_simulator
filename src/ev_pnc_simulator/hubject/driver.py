"""Hubject calling the eMSP under test: the operations of the EMP contract that the hub sends.

authorize start and stop, charge detail record, reservation stop and charging notifications are requests
*to* the eMSP. The mock sends them on demand (`POST /_mock/drive/<key>`), checks that what it sent is valid
against the pinned request schema, and checks the eMSP's answer against the response schema.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .. import schema
from .oicp import Oicp, Op

# Defaults for the path parameters of the driven operations. They only have to satisfy the pinned patterns.
PATH_DEFAULTS = {"operatorID": "TR*ZES", "providerID": "TRTST", "externalID": "TRTST"}


def expand(op: Op, params: dict[str, str]) -> str:
    path = op.path
    for name in [p for p in _names(path)]:
        value = params.get(name, PATH_DEFAULTS.get(name, "x"))
        path = path.replace("{" + name + "}", quote(value, safe=""))
    return path


def _names(path: str) -> list[str]:
    out, i = [], 0
    while (i := path.find("{", i)) != -1:
        j = path.index("}", i)
        out.append(path[i + 1 : j])
        i = j
    return out


async def drive(
    oicp: Oicp,
    client: httpx.AsyncClient,
    key: str,
    *,
    target: str,
    body: dict | None = None,
    path_params: dict | None = None,
    headers: dict | None = None,
) -> dict[str, Any]:
    op = oicp.by_key(key)
    if op is None or op.role != "driven":
        raise KeyError(key)
    sent = body if body is not None else schema.sample(oicp.doc, op.request_schema)
    req_v, res_v = oicp._validators[op.key]
    request_problems = schema.errors(req_v, sent)
    url = target.rstrip("/") + expand(op, path_params or {})
    resp = await client.post(url, json=sent, headers=headers or {})
    try:
        answer: Any = resp.json()
    except ValueError:
        answer = None
    response_problems = schema.errors(res_v, answer) if answer is not None else ["response is not JSON"]
    return {
        "operation": op.key,
        "url": url,
        "sent": sent,
        "request_valid": not request_problems,
        "request_problems": request_problems,
        "status": resp.status_code,
        "response": answer,
        "response_valid": resp.status_code == 200 and not response_problems,
        "response_problems": response_problems,
    }
