"""Hubject mock, OPCP side: every operation, every declared error code, the webhook service."""

import re

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ev_pnc_simulator.hubject.app import create_app
from ev_pnc_simulator.hubject.webhooks import SIGNATURE_HEADERS, event_types, registrable_events, sign, verify

AUTH = {"Authorization": "Bearer test"}


def real_path(op):
    return re.sub(r"\{[^}]+\}", "abc", op.path)


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def ops(client):
    return client.app.state.opcp.ops


def call(client, op, headers=AUTH, **kw):
    return client.request(op.method, real_path(op), headers=headers, **kw)


def test_all_opcp_operations_are_loaded(ops):
    assert len(ops) == 22
    assert {o.service for o in ops} == {"ccp", "cps", "est", "pcp", "rcp", "webhooks"}


def test_every_non_webhook_operation_answers_with_its_first_success_code(client, ops):
    for op in [o for o in ops if o.service != "webhooks"]:
        r = call(client, op)
        want = int(min((c for c in op.responses if c.startswith("2")), key=int))
        assert r.status_code == want, (op.operation_id, r.status_code, r.text[:200])


def test_missing_bearer_token_is_a_401_with_the_error_shape(client, ops):
    r = call(client, ops[0], headers={})
    assert r.status_code == 401
    assert r.json()["errorMessages"] and re.fullmatch(r"[0-9a-f-]{36}", r.json()["traceId"])


def test_auth_check_can_be_switched_off():
    with TestClient(create_app(require_auth=False)) as c:
        assert c.get("/v1/root/rootCerts").status_code == 200


def test_every_declared_error_code_can_be_injected_on_every_operation(client, ops):
    checked = 0
    for op in ops:
        for declared in op.declared_codes():
            if declared.startswith("2"):
                continue
            status = {"5XX": 502}.get(declared) or int(declared)
            client.delete("/_mock/rules")
            client.post("/_mock/rules", json={"operation": op.operation_id, "do": {"http_status": status}, "times": 1})
            r = call(client, op)
            assert r.status_code == status, (op.operation_id, declared)
            if op.service != "est":
                assert r.json()["errorMessages"], (op.operation_id, declared)
            checked += 1
    assert checked >= 100


def test_est_5xx_family_accepts_any_5xx(client, ops):
    est = next(o for o in ops if o.operation_id == "simpleenroll")
    for status in (500, 502, 503):
        client.post("/_mock/rules", json={"operation": "simpleenroll", "do": {"http_status": status}, "times": 1})
        assert call(client, est).status_code == status


def test_an_injected_body_and_a_service_prefixed_name_work(client, ops):
    client.post(
        "/_mock/rules",
        json={
            "operation": "ccp:lookupContract",
            "do": {"http_status": 404, "body": {"errorMessages": ["no contract"], "traceId": "t"}},
            "times": 1,
        },
    )
    op = next(o for o in ops if o.operation_id == "lookupContract")
    r = call(client, op)
    assert r.status_code == 404 and r.json()["errorMessages"] == ["no contract"]


# ---- webhook service ---------------------------------------------------------------------------------------


def create(client, **kw):
    body = {"url": "http://receiver/hooks", "enabledEvents": ["root.cert.added"], **kw}
    return client.post("/v1/webhook/endpoints", json=body, headers=AUTH)


def test_webhook_endpoint_lifecycle(client):
    r = create(client)
    assert r.status_code == 201
    ep = r.json()
    assert ep["status"] == "enabled" and ep["secret"]
    listed = client.get("/v1/webhook/endpoints", headers=AUTH).json()
    assert [e["id"] for e in listed] == [ep["id"]] and "secret" not in listed[0]
    assert "secret" not in client.get(f"/v1/webhook/endpoints/{ep['id']}", headers=AUTH).json()
    upd = client.put(f"/v1/webhook/endpoints/{ep['id']}", json={"status": "disabled"}, headers=AUTH).json()
    assert upd["status"] == "disabled"
    assert client.delete(f"/v1/webhook/endpoints/{ep['id']}", headers=AUTH).status_code == 200
    assert client.get(f"/v1/webhook/endpoints/{ep['id']}", headers=AUTH).status_code == 404


def test_webhook_registration_rejects_bad_input(client):
    assert client.post("/v1/webhook/endpoints", json={"url": "http://x"}, headers=AUTH).status_code == 400
    r = create(client, enabledEvents=["not.an.event"])
    assert r.status_code == 400 and "not.an.event" in r.json()["errorMessages"][0]
    assert client.put("/v1/webhook/endpoints/nope", json={}, headers=AUTH).status_code == 404


def test_event_names_in_the_two_sources_disagree_and_the_mock_knows_both():
    # Registration API: mo.prov.cert.updated.factory.reset; AsyncAPI profile: mo.prov.cert.factory.reset.
    assert "mo.prov.cert.updated.factory.reset" in registrable_events()
    assert "mo.prov.cert.factory.reset" in event_types() - registrable_events()


def receiver():
    seen = []

    async def hooks(request: Request):
        seen.append({"raw": await request.body(), "headers": dict(request.headers)})
        return JSONResponse({"ok": True}, status_code=seen[-1].get("status", 200))

    app = Starlette(routes=[Route("/hooks", hooks, methods=["POST"])])
    return app, seen


def hub_with_receiver():
    rx, seen = receiver()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=rx), base_url="http://receiver")
    return TestClient(create_app(http_client=http)), seen


def test_signed_event_reaches_the_receiver_and_verifies():
    c, seen = hub_with_receiver()
    with c:
        ep = create(c).json()
        secret = c.get(f"/_mock/webhooks/{ep['id']}/secret").json()["secret"]
        payload = {
            "rootCertificateId": "0b6e9c7e-0000-4000-8000-000000000001",
            "distinguishedName": "CN=x",
            "commonName": "x",
            "rootType": "MO",
        }
        out = c.post(
            f"/_mock/webhooks/{ep['id']}/emit", json={"event_type": "root.cert.added", "payload": payload}
        ).json()
        assert (
            out["status"] == 200 and out["sent"]["eventType"] == "root.cert.added" and out["sent"]["payload"] == payload
        )
        got = seen[0]
        header = got["headers"]["x-hubject-signature"]
        assert header.startswith("sha256=") and verify(secret, got["raw"], header)
        assert not verify("another-secret", got["raw"], header)
    assert sign(secret, b"body") != sign(secret, b"body ")


@pytest.mark.parametrize("header", SIGNATURE_HEADERS)
def test_every_known_signature_header_name_can_be_used(header):
    c, seen = hub_with_receiver()
    with c:
        ep = create(c).json()
        c.post(
            f"/_mock/webhooks/{ep['id']}/emit",
            json={
                "event_type": "oem.contract.created",
                "payload": {"emaid": "TRTSTC000000011", "pcid": "p"},
                "signature_header": header,
            },
        )
        assert header.lower() in seen[0]["headers"]


def test_a_corrupted_signature_fails_verification():
    c, seen = hub_with_receiver()
    with c:
        ep = create(c).json()
        secret = c.get(f"/_mock/webhooks/{ep['id']}/secret").json()["secret"]
        c.post(
            f"/_mock/webhooks/{ep['id']}/emit",
            json={"event_type": "root.cert.added", "payload": {}, "corrupt_signature": True},
        )
        got = seen[0]
        assert not verify(secret, got["raw"], got["headers"]["x-hubject-signature"])


def test_emit_refuses_unknown_events_endpoints_and_headers(client):
    ep = create(client).json()
    emit = f"/_mock/webhooks/{ep['id']}/emit"
    assert client.post(emit, json={"event_type": "nope"}).status_code == 422
    assert client.post(emit, json={"event_type": "root.cert.added", "signature_header": "X-Other"}).status_code == 422
    assert client.post("/_mock/webhooks/missing/emit", json={"event_type": "root.cert.added"}).status_code == 404
    assert client.get("/_mock/webhooks/missing/secret").status_code == 404


def test_credentials_in_request_headers_are_masked_in_the_log(client):
    create(client)
    log = str(client.get("/_mock/requests").json())
    assert "Bearer test" not in log and "'authorization': '***'" in log
