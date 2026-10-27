"""Remote commands on the GIREVE mock: the synchronous answer and the asynchronous CommandResult."""

from __future__ import annotations

import pytest
from gireve_support import EMSP_TOKEN, FakeEmsp, V, as_emsp, b64, mock_for, push_token, register
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

LOCATION, EVSE = "LOC0001", "FR*ABC*E10001"


@pytest.fixture
def emsp():
    return FakeEmsp()


@pytest.fixture
def c(emsp):
    with mock_for(emsp) as client:
        assert register(client)["registered"]
        assert push_token(client).json()["status_code"] == 1000
        yield client


def start_body(**over):
    body = {
        "response_url": "http://emsp/ocpi/command-result/1",
        "token": {"country_code": "TR", "party_id": "TST", "uid": "04A1B2C3D4E5F6"},
        "location_id": LOCATION,
        "evse_uid": EVSE,
        **over,
    }
    return {k: v for k, v in body.items() if v is not None}


def post(c, name, body):
    return c.post(f"{V}/commands/{name}", json=body, headers=as_emsp(c))


def test_start_session_is_accepted_and_the_result_is_posted_to_the_response_url(c, emsp):
    out = post(c, "START_SESSION", start_body()).json()
    assert out["status_code"] == 1000 and out["data"]["result"] == "ACCEPTED"
    results = [s for s in emsp.seen if s["path"].startswith("/ocpi/command-result")]
    assert len(results) == 1
    got = results[0]
    assert got["body"]["result"] == "ACCEPTED"
    assert got["headers"]["authorization"] == b64(EMSP_TOKEN)
    assert got["headers"]["ocpi-to-party-id"] == "TST" and got["headers"]["ocpi-from-party-id"] == "ABC"
    assert got["headers"]["x-request-id"] and got["headers"]["x-correlation-id"]
    cmd = next(iter(c.app.state.hub.commands.values()))
    assert cmd["delivered"] == {"result": "ACCEPTED", "status": 200}


def test_evse_uid_is_required_on_start_session(c):
    out = post(c, "START_SESSION", start_body(evse_uid=None)).json()
    assert out["status_code"] == 2001 and "evse_uid" in out["status_message"]


def test_unknown_location_unknown_token_and_missing_response_url(c):
    assert post(c, "START_SESSION", start_body(location_id="NOPE")).json()["status_code"] == 2003
    assert post(c, "START_SESSION", start_body(evse_uid="FR*ABC*E99999")).json()["status_code"] == 2003
    token = {"country_code": "TR", "party_id": "TST", "uid": "FFFFFFFF"}
    assert post(c, "START_SESSION", start_body(token=token)).json()["status_code"] == 2004
    body = start_body()
    del body["response_url"]
    assert post(c, "START_SESSION", body).json()["status_code"] == 2001


@pytest.mark.parametrize("name", ["RESERVE_NOW", "CANCEL_RESERVATION", "UNLOCK_CONNECTOR"])
def test_commands_gireve_does_not_support_answer_not_supported(c, emsp, name):
    out = post(c, name, start_body()).json()
    assert out["status_code"] == 1000 and out["data"]["result"] == "NOT_SUPPORTED"
    assert not [s for s in emsp.seen if s["path"].startswith("/ocpi/command-result")]


def test_unknown_command_name_is_a_parameter_error(c):
    assert post(c, "REBOOT", start_body()).json()["status_code"] == 2001


def test_stop_session_needs_a_session_the_mock_started(c, emsp):
    assert (
        post(c, "STOP_SESSION", {"response_url": "http://emsp/ocpi/command-result/2", "session_id": "nope"}).json()[
            "data"
        ]["result"]
        == "UNKNOWN_SESSION"
    )
    post(c, "START_SESSION", start_body())
    sid = next(iter(c.app.state.hub.sessions))
    out = post(c, "STOP_SESSION", {"response_url": "http://emsp/ocpi/command-result/2", "session_id": sid}).json()
    assert out["data"]["result"] == "ACCEPTED"
    assert len([s for s in emsp.seen if s["path"].startswith("/ocpi/command-result")]) == 2


def test_a_rule_can_make_the_cpo_side_report_a_failure(c, emsp):
    rule = {"operation": "command", "do": {"command_result": "REJECTED"}, "times": 1}
    assert c.post("/_mock/rules", json=rule).status_code == 201
    out = post(c, "START_SESSION", start_body()).json()
    assert out["data"]["result"] == "ACCEPTED"  # the synchronous answer is unchanged, as in OCPI
    last = [s for s in emsp.seen if s["path"].startswith("/ocpi/command-result")][-1]
    assert last["body"]["result"] == "REJECTED"
    assert post(c, "START_SESSION", start_body()).json()["data"]["result"] == "ACCEPTED"
    assert [s for s in emsp.seen if s["path"].startswith("/ocpi/command-result")][-1]["body"]["result"] == "ACCEPTED"


def test_held_results_are_delivered_on_demand():
    emsp = FakeEmsp()
    with mock_for(emsp, auto_callbacks=False) as c:
        register(c)
        push_token(c)
        post(c, "START_SESSION", start_body())
        assert not [s for s in emsp.seen if s["path"].startswith("/ocpi/command-result")]
        (cmd,) = c.get("/_mock/commands").json()
        assert cmd["delivered"] is None
        out = c.post(f"/_mock/commands/{cmd['uid']}/deliver", json={"result": "TIMEOUT"}).json()
        assert out == {"result": "TIMEOUT", "status": 200}
        assert c.post("/_mock/commands/nope/deliver", json={}).status_code == 404


def test_a_failing_response_url_is_recorded_not_raised():
    emsp = FakeEmsp()
    with mock_for(emsp) as c:
        register(c)
        push_token(c)
        failing = Starlette(routes=[Route("/ocpi/command-result/{uid}", _fail, methods=["POST"])])
        c.app.state.hub.client._transport.app = failing  # from now on the IOP talks to an eMSP that answers 500
        post(c, "START_SESSION", start_body())
        (cmd,) = c.get("/_mock/commands").json()
        assert cmd["delivered"] == {"result": "ACCEPTED", "status": 500}


async def _fail(request):
    return JSONResponse({}, status_code=500)
