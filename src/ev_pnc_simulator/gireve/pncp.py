"""GIREVE PNCP webhooks: events GIREVE POSTs to an eMSP endpoint, signed with HMAC-SHA256.

Signature scheme from the profile's AsyncAPI: Base64 of the HMAC-SHA256 of the raw payload with the webhook
secret, in the `payload-signature` header. Event names come from the pinned AsyncAPI. The payload schemas are
only partly known upstream (x-unverified), so the payload is whatever the caller sends; the mock adds the
event name and a timestamp.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx

from .. import contracts

EVENTS_FILE = "gireve-pncp-events.asyncapi.yaml"
HEADER = "payload-signature"


def sign(secret: str, raw: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()


def verify(secret: str, raw: bytes, value: str) -> bool:
    """What an eMSP must do: constant-time compare of the received value with its own computation."""
    return hmac.compare_digest(sign(secret, raw), value)


def event_types() -> set[str]:
    doc = contracts.profile(EVENTS_FILE)
    out: set[str] = set()
    for msg in doc["components"]["messages"].values():
        out |= set(msg["payload"]["properties"]["eventtype"]["enum"])
    return out


async def emit(
    client: httpx.AsyncClient,
    *,
    target: str,
    secret: str,
    event_type: str,
    data: dict | None = None,
    corrupt_signature: bool = False,
) -> dict:
    envelope = {
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "eventtype": event_type,
        "data": data or {},
    }
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    value = sign(secret, raw)
    if corrupt_signature:
        value = ("A" if value[0] != "A" else "B") + value[1:]
    resp = await client.post(target, content=raw, headers={"Content-Type": "application/json", HEADER: value})
    return {"sent": envelope, "status": resp.status_code, "response_body": resp.text[:2000]}
