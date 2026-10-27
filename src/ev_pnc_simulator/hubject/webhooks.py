"""OPCP webhook service: endpoint registry (the API in hubject-opcp-webhooks) and signed event delivery.

Signing follows the profile's AsyncAPI: HMAC-SHA256 of the raw body with the endpoint secret, sent as
`sha256=<hex>`. The sources disagree on the header name (X-Hubject-Signature, X-Operator-Signature,
payload-signature), so the mock can send any of them. Retries (3, one hour apart, on 5xx) are not simulated;
the delivery result is returned so a test can drive a retry itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from .. import contracts, schema

SIGNATURE_HEADERS = ("X-Hubject-Signature", "X-Operator-Signature", "payload-signature")
EVENTS_FILE = "hubject-opcp-events.asyncapi.yaml"
WEBHOOKS_FILE = "hubject-opcp-webhooks.mo.openapi.json"


def sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def verify(secret: str, raw: bytes, header_value: str) -> bool:
    """What an eMSP must do: constant-time compare of the received value with its own computation."""
    return hmac.compare_digest(sign(secret, raw), header_value)


def event_types() -> set[str]:
    """Event names from the profile's AsyncAPI plus the names the registration API accepts."""
    doc = contracts.profile(EVENTS_FILE)
    out: set[str] = set()
    for msg in doc["components"]["messages"].values():
        out |= set(msg["payload"]["properties"]["eventType"]["enum"])
    return out | registrable_events()


def registrable_events() -> set[str]:
    """Event names the registration API accepts in `enabledEvents`."""
    api = contracts.source(WEBHOOKS_FILE)
    body = schema.inline(
        api, api["paths"]["/v1/webhook/endpoints"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    )
    return set(body["properties"]["enabledEvents"]["items"]["enum"])


class Webhooks:
    def __init__(self) -> None:
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.enabled_events = registrable_events()

    def reset(self) -> None:
        self.endpoints.clear()

    def create(self, body: dict) -> tuple[int, dict]:
        url = body.get("url")
        events = body.get("enabledEvents")
        if not url or not events:
            return 400, {"errorMessages": ["url and enabledEvents are required"], "traceId": str(uuid.uuid4())}
        unknown = [e for e in events if e not in self.enabled_events]
        if unknown:
            return 400, {"errorMessages": [f"unknown event type: {e}" for e in unknown], "traceId": str(uuid.uuid4())}
        ep = {
            "id": str(uuid.uuid4()),
            "apiVersion": body.get("apiVersion", "1.0.0"),
            "created": int(datetime.now(UTC).timestamp()),
            "description": body.get("description", ""),
            "enabledEvents": events,
            "metadata": body.get("metadata", {}),
            "status": body.get("status", "enabled"),
            "url": url,
            "secret": secrets.token_hex(16),
            "providerIds": body.get("providerIds", []),
        }
        self.endpoints[ep["id"]] = ep
        return 201, ep

    def update(self, ep_id: str, body: dict) -> tuple[int, dict]:
        ep = self.endpoints.get(ep_id)
        if ep is None:
            return 404, {"errorMessages": ["no such endpoint"], "traceId": str(uuid.uuid4())}
        ep.update(
            {
                k: v
                for k, v in body.items()
                if k in ("apiVersion", "description", "enabledEvents", "metadata", "status", "url", "providerIds")
            }
        )
        return 200, ep

    async def emit(
        self,
        client: httpx.AsyncClient,
        *,
        endpoint_id: str,
        event_type: str,
        payload: dict,
        header: str = SIGNATURE_HEADERS[0],
        corrupt_signature: bool = False,
        xsd: str = "urn:iso:15118:2:2013:MsgDef",
    ) -> dict:
        """Send one signed event to a registered endpoint and report what the receiver answered."""
        ep = self.endpoints.get(endpoint_id)
        if ep is None:
            raise KeyError(endpoint_id)
        if header not in SIGNATURE_HEADERS:
            raise ValueError(f"header must be one of {SIGNATURE_HEADERS}")
        envelope = {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "eventId": str(uuid.uuid4()),
            "eventType": event_type,
            "xsdMsgDefNamespace": xsd,
            "payload": payload,
        }
        raw = json.dumps(envelope, separators=(",", ":")).encode()
        value = sign(ep["secret"], raw)
        if corrupt_signature:
            value = value[:-4] + ("0000" if not value.endswith("0000") else "ffff")
        resp = await client.post(ep["url"], content=raw, headers={"Content-Type": "application/json", header: value})
        return {
            "sent": envelope,
            "signature_header": header,
            "status": resp.status_code,
            "response_body": resp.text[:2000],
        }
