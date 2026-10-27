"""GIREVE mock calling the eMSP (registration, authorize, pushes), failure injection and PNCP webhooks."""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import pytest
from gireve_support import FakeEmsp, V, as_emsp, mock_for, push_token, register, rfid_token
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ev_pnc_simulator.gireve import pncp


@pytest.fixture
def emsp():
    return FakeEmsp()


@pytest.fixture
def c(emsp):
    with mock_for(emsp) as client:
        assert register(client)["registered"]
        yield client


def drive(c, key, **spec):
    return c.post(f"/_mock/drive/{key}", json=spec).json()


def test_driving_before_registration_is_a_conflict(emsp):
    with mock_for(emsp) as c:
        r = c.post("/_mock/drive/authorize", json={})
    assert r.status_code == 409 and "register" in r.json()["error"]


def test_unknown_driven_operation(c):
    assert c.post("/_mock/drive/launch", json={}).status_code == 404


@pytest.mark.parametrize(
    "key, method, path_prefix",
    [
        ("location-put", "PUT", f"{V}/locations/FR/ABC/LOC0001"),
        ("tariff-put", "PUT", f"{V}/tariffs/FR/ABC/TAR001"),
        ("session-put", "PUT", f"{V}/sessions/FR/ABC/SES0001"),
        ("session-patch", "PATCH", f"{V}/sessions/FR/ABC/SES0001"),
        ("cdr-post", "POST", f"{V}/cdrs"),
    ],
)
def test_pushes_use_the_emsp_endpoints_the_iop_learned_at_registration(c, emsp, key, method, path_prefix):
    out = drive(c, key)
    assert out["ok"], out
    last = emsp.seen[-1]
    assert (last["method"], last["path"]) == (method, path_prefix)
    # module calls carry the token the eMSP issued (Base64) and routing + tracing headers
    h = last["headers"]
    assert base64.b64decode(h["authorization"].removeprefix("Token ")).decode() == "EMSP-FINAL-TOKEN"
    assert h["ocpi-from-country-code"] == "FR" and h["ocpi-to-party-id"] == "TST"
    assert h["x-request-id"] and h["x-correlation-id"]


def test_authorize_sends_a_location_reference_and_checks_the_answer(c, emsp):
    out = drive(c, "authorize", token_uid="04A1B2C3D4E5F6")
    assert out["ok"] and out["response"]["data"]["allowed"] == "ALLOWED"
    last = emsp.seen[-1]
    assert last["path"].endswith("/tokens/04A1B2C3D4E5F6/authorize") and last["query"] == {"type": "RFID"}
    assert last["body"]["location_id"] == "LOC0001"


def test_authorize_flags_an_answer_that_is_not_an_authorization_info():
    with mock_for(FakeEmsp(authorize_status="MAYBE")) as c:
        register(c)
        out = drive(c, "authorize")
    assert not out["ok"] and any("allowed" in p for p in out["problems"])


def test_a_missing_tracing_echo_is_reported():
    with mock_for(FakeEmsp(echo_ids=False)) as c:
        register(c)
        out = drive(c, "location-put")
    assert not out["ok"]
    assert sum("not echoed" in p for p in out["problems"]) == 2


def test_the_cdr_remark_tells_who_priced_it(c, emsp):
    drive(c, "cdr-post", remark="[?] price not significant")
    assert emsp.seen[-1]["body"]["remark"] == "[?] price not significant"
    drive(c, "cdr-post")
    assert emsp.seen[-1]["body"]["remark"].startswith("[*]")


def test_a_body_of_your_own_replaces_the_fixture(c, emsp):
    drive(c, "tariff-put", body={"id": "TAR777", "currency": "EUR"})
    assert emsp.seen[-1]["path"].endswith("/FR/ABC/TAR777")
    assert emsp.seen[-1]["body"] == {"id": "TAR777", "currency": "EUR"}


def test_unregister_deletes_the_credentials_and_closes_the_door(c, emsp):
    out = drive(c, "unregister")
    assert out["registered"] is False and out["steps"][0]["ok"]
    assert emsp.seen[-1]["method"] == "DELETE"
    stale = {**as_emsp_headers(), "Authorization": "Token " + base64.b64encode(b"old").decode()}
    assert c.get(f"{V}/locations", headers=stale).status_code == 401


def as_emsp_headers() -> dict:
    return {
        "X-Request-ID": "r",
        "X-Correlation-ID": "c",
        "OCPI-from-country-code": "TR",
        "OCPI-from-party-id": "TST",
        "OCPI-to-country-code": "FR",
        "OCPI-to-party-id": "ABC",
    }


def test_unregister_when_not_registered_is_a_no_op(emsp):
    with mock_for(emsp) as c:
        assert drive(c, "unregister")["note"] == "not registered"


def test_an_emsp_that_cannot_be_reached_is_a_502():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    from ev_pnc_simulator.gireve.app import create_app

    with TestClient(create_app(http_client=http)) as c:
        r = c.post("/_mock/drive/register", json={"versions_url": "http://nowhere/ocpi/versions", "temp_token": "t"})
    assert r.status_code == 502


# ---- failure injection -------------------------------------------------------------------------------------------


def rule(c, **spec):
    r = c.post("/_mock/rules", json=spec)
    assert r.status_code == 201, r.text


def test_an_ocpi_status_rule_answers_with_http_200_and_the_chosen_code(c):
    rule(c, operation="locations-get", do={"ocpi_status": 3000, "message": "hub down"}, times=1)
    first = c.get(f"{V}/locations", headers=as_emsp(c))
    assert (
        first.status_code == 200
        and first.json()["status_code"] == 3000
        and first.json()["status_message"] == "hub down"
    )
    assert c.get(f"{V}/locations", headers=as_emsp(c)).json()["status_code"] == 1000


def test_rate_limit_and_http_status_rules_use_the_shared_actions(c):
    rule(c, operation="tariffs-get", do={"rate_limit": {"retry_after_s": 7}}, times=1)
    r = c.get(f"{V}/tariffs", headers=as_emsp(c))
    assert r.status_code == 429 and r.headers["retry-after"] == "7"
    rule(c, operation="tariffs-get", do={"http_status": 503})
    assert c.get(f"{V}/tariffs", headers=as_emsp(c)).status_code == 503


def test_rules_can_match_on_the_token_in_the_body(c):
    rule(c, operation="token-put", when={"uid": "04A1B2C3"}, do={"ocpi_status": 2001, "message": "nope"})

    assert push_token(c, rfid_token("04A1B2C3")).json()["status_code"] == 2001
    assert push_token(c, rfid_token("04A1B2C3D4E5F6")).json()["status_code"] == 1000


def test_a_rule_needs_an_action_and_a_known_operation(c):
    assert c.post("/_mock/rules", json={"operation": "nonsense", "do": {"ocpi_status": 2001}}).status_code == 422
    assert c.post("/_mock/rules", json={"operation": "command", "do": {"message": "x"}}).status_code == 422
    assert (
        c.post(
            "/_mock/rules", json={"operation": "command", "do": {"ocpi_status": 2001, "http_status": 500}}
        ).status_code
        == 422
    )
    assert (
        c.post(
            "/_mock/rules", json={"operation": "command", "match": {"uid": "x"}, "do": {"ocpi_status": 2001}}
        ).status_code
        == 422
    )


def test_the_request_log_masks_tokens_and_health_reports_the_profile_hash(c):
    c.get(f"{V}/locations", headers=as_emsp(c))
    log = c.get("/_mock/requests", params={"operation": "locations-get"}).json()
    assert log and log[-1]["headers"]["authorization"] == "***"
    health = c.get("/_mock/health").json()
    assert health["mock"] == "gireve" and health["registered"] is True
    assert len(next(iter(health["profile"].values()))) == 64
    assert "sessions-get" in health["served"] and "cdr-post" in health["driven"]


def test_reset_forgets_the_registration(c):
    assert c.post("/_mock/reset").status_code == 204
    assert c.get("/_mock/health").json()["registered"] is False


# ---- PNCP webhooks -----------------------------------------------------------------------------------------------


def receiver(secret: str):
    """An eMSP webhook endpoint that verifies the signature as the PNCP guide says it must."""
    seen = []

    async def hook(request: Request):
        raw = await request.body()
        ok = pncp.verify(secret, raw, request.headers.get(pncp.HEADER, ""))
        seen.append({"ok": ok, "body": await request.json()})
        return JSONResponse({}, status_code=200 if ok else 401)

    return Starlette(routes=[Route("/pncp/webhooks", hook, methods=["POST"])]), seen


def hub_with(app):
    from ev_pnc_simulator.gireve.app import create_app

    return TestClient(
        create_app(http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://emsp"))
    )


def test_signature_is_base64_hmac_sha256_of_the_raw_body():
    raw = b'{"a":1}'
    expected = base64.b64encode(hmac.new(b"s3cret", raw, hashlib.sha256).digest()).decode()
    assert pncp.sign("s3cret", raw) == expected
    assert pncp.verify("s3cret", raw, expected) and not pncp.verify("other", raw, expected)


def test_event_types_come_from_the_pinned_asyncapi():
    names = pncp.event_types()
    assert "root.certificate.available" in names and len(names) >= 4
    assert all(n.count(".") >= 2 for n in names)


def test_a_signed_event_is_accepted_and_a_corrupted_one_is_refused():
    app, seen = receiver("topsecret")
    with hub_with(app) as c:
        spec = {
            "target": "http://emsp/pncp/webhooks",
            "secret": "topsecret",
            "event_type": "root.certificate.available",
            "data": {"type": "V2G"},
        }
        good = c.post("/_mock/pncp/emit", json=spec).json()
        bad = c.post("/_mock/pncp/emit", json={**spec, "corrupt_signature": True}).json()
        wrong_secret = c.post("/_mock/pncp/emit", json={**spec, "secret": "nope"}).json()
    assert good["status"] == 200 and bad["status"] == 401 and wrong_secret["status"] == 401
    assert [s["ok"] for s in seen] == [True, False, False]
    assert seen[0]["body"]["eventtype"] == "root.certificate.available" and seen[0]["body"]["data"] == {"type": "V2G"}


def test_emit_validates_its_input():
    app, _ = receiver("x")
    with hub_with(app) as c:
        assert c.post("/_mock/pncp/emit", json={}).status_code == 422
        r = c.post(
            "/_mock/pncp/emit", json={"target": "http://emsp/pncp/webhooks", "secret": "x", "event_type": "made.up"}
        )
        assert r.status_code == 422 and "known" in r.json()
