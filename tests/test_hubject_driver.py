"""Hubject mock calling an eMSP: the five operations of the contract that the hub sends."""

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ev_pnc_simulator.hubject.app import create_app

DRIVEN = ["authorize-start", "authorize-stop", "cdr-push", "reservation-stop", "charging-notifications"]


def emsp(answer):
    """A stand-in eMSP: answers every request with `answer(request_json)` and remembers what it got."""
    seen = []

    async def handle(request):
        body = await request.json()
        seen.append({"path": request.url.path, "body": body})
        out = answer(body)
        return out if not isinstance(out, dict) else JSONResponse(out)

    paths = [
        "/charging/v21/operators/{operatorID}/authorize/start",
        "/charging/v21/operators/{operatorID}/authorize/stop",
        "/cdrmgmt/v22/operators/{operatorID}/charge-detail-record",
        "/reservation/v11/providers/{providerID}/reservation-stop-request",
        "/notificationmgmt/v11/charging-notifications",
    ]
    return Starlette(routes=[Route(p, handle, methods=["POST"]) for p in paths]), seen


def ack(_):
    return {"Result": True, "StatusCode": {"Code": "000"}}


def authorized(_):
    return {"AuthorizationStatus": "Authorized", "StatusCode": {"Code": "000"}}


def hub(app):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://emsp")
    return TestClient(create_app(http_client=http))


@pytest.mark.parametrize(
    "key, answer",
    [
        ("authorize-start", authorized),
        ("authorize-stop", authorized),
        ("cdr-push", ack),
        ("reservation-stop", ack),
        ("charging-notifications", ack),
    ],
)
def test_hub_sends_a_valid_request_and_checks_the_answer(key, answer):
    app, seen = emsp(answer)
    with hub(app) as c:
        out = c.post(f"/_mock/drive/{key}", json={"target": "http://emsp"}).json()
    assert out["request_valid"] and out["response_valid"], out
    assert out["status"] == 200 and len(seen) == 1


def test_path_parameters_are_expanded_and_the_body_can_be_given():
    app, seen = emsp(authorized)
    with hub(app) as c:
        body = {"OperatorID": "TR*ZES", "Identification": {"RFIDMifareFamilyIdentification": {"UID": "04A1B2C3"}}}
        out = c.post(
            "/_mock/drive/authorize-start",
            json={"target": "http://emsp/", "body": body, "path_params": {"operatorID": "TR*ZES"}},
        ).json()
    assert (
        seen[0]["path"] == "/charging/v21/operators/TR%2AZES/authorize/start"
        or seen[0]["path"] == "/charging/v21/operators/TR*ZES/authorize/start"
    )
    assert seen[0]["body"] == body and out["request_valid"]


def test_a_request_that_breaks_the_contract_is_reported_not_hidden():
    app, _ = emsp(ack)
    with hub(app) as c:
        out = c.post(
            "/_mock/drive/cdr-push", json={"target": "http://emsp", "body": {"EvseID": "TR*ZES*E10001"}}
        ).json()
    assert out["request_valid"] is False and any("SessionID" in p for p in out["request_problems"])


def test_an_answer_that_breaks_the_contract_is_reported():
    app, _ = emsp(lambda _: {"Result": "maybe"})
    with hub(app) as c:
        out = c.post("/_mock/drive/cdr-push", json={"target": "http://emsp"}).json()
    assert out["response_valid"] is False and out["response_problems"]


def test_a_non_json_or_failing_emsp_is_reported():
    app, _ = emsp(lambda _: PlainTextResponse("boom", status_code=500))
    with hub(app) as c:
        out = c.post("/_mock/drive/cdr-push", json={"target": "http://emsp"}).json()
    assert out["status"] == 500 and out["response_valid"] is False


def test_only_hub_sent_operations_can_be_driven():
    app, _ = emsp(ack)
    with hub(app) as c:
        assert c.post("/_mock/drive/remote-start", json={"target": "http://emsp"}).status_code == 404
        assert c.post("/_mock/drive/nope", json={"target": "http://emsp"}).status_code == 404
        health = c.get("/_mock/health").json()
    assert sorted(health["oicp"]["driven"]) == sorted(DRIVEN)


def test_drive_needs_a_target():
    app, _ = emsp(ack)
    with hub(app) as c:
        assert c.post("/_mock/drive/cdr-push", json={}).status_code == 422


def test_an_unreachable_target_is_a_502():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    with TestClient(create_app(http_client=http)) as c:
        r = c.post("/_mock/drive/cdr-push", json={"target": "http://nowhere"})
    assert r.status_code == 502
