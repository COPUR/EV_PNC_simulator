"""Hubject mock, OICP side: coverage of the contract, default behaviour, failure injection."""

from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from ev_pnc_simulator import schema
from ev_pnc_simulator.hubject.app import create_app

START = "/charging/v21/providers/TRTST/authorize-remote/start"
STOP = "/charging/v21/providers/TRTST/authorize-remote/stop"
CDRS = "/cdrmgmt/v22/providers/TRTST/get-charge-detail-records-request"
EVCO = {"RemoteIdentification": {"EvcoID": "TR-TST-C00000001-1"}}
WIDE = {"ProviderID": "TRTST", "From": "2020-01-01T00:00:00.000Z", "To": "2099-01-01T00:00:00.000Z"}


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def start(client, evse="TR*ZES*E10001"):
    return client.post(START, json={"ProviderID": "TRTST", "EvseID": evse, "Identification": EVCO})


def code(resp):
    return resp.json()["StatusCode"]["Code"]


def sample_request(oicp, op):
    return schema.sample(oicp.doc, op.request_schema)


def path_of(op):
    return op.path.replace("{operatorID}", "TR*ZES").replace("{providerID}", "TRTST").replace("{externalID}", "TRTST")


# ---- coverage ---------------------------------------------------------------------------------------------


def test_every_operation_of_the_contract_is_served_or_driven(app):
    oicp = app.state.oicp
    served, driven = {o.key for o in oicp.served()}, {o.key for o in oicp.driven()}
    assert len(oicp.ops) == 14
    assert not served & driven
    assert served | driven == {o.key for o in oicp.ops}
    assert driven == {"authorize-start", "authorize-stop", "cdr-push", "reservation-stop", "charging-notifications"}


def test_status_code_list_is_read_from_the_contract(app):
    codes = app.state.oicp.codes
    expected = [
        "000",
        "001",
        "002",
        "009",
        "017",
        "018",
        "019",
        "021",
        "022",
        "101",
        "102",
        "103",
        "105",
        "106",
        "110",
        "120",
        "121",
        "122",
        "200",
        "210",
        "300",
        "310",
        "320",
        "400",
        "501",
        "510",
        "601",
        "602",
        "603",
        "604",
        "700",
    ]
    assert sorted(codes) == sorted(expected)
    assert codes["603"] == "Unknown EVSE ID"


def test_every_served_operation_answers_200_with_a_valid_body(app, client):
    oicp = app.state.oicp
    for op in oicp.served():
        resp = client.post(path_of(op), json=sample_request(oicp, op))
        assert resp.status_code == 200, (op.key, resp.text)
        problems = schema.errors(oicp._validators[op.key][1], resp.json())
        assert not problems, (op.key, problems)


# ---- default behaviour ------------------------------------------------------------------------------------


def test_remote_start_and_stop_and_the_cdr(client):
    r = start(client)
    assert r.status_code == 200 and r.json()["Result"] is True and code(r) == "000"
    session = r.json()["SessionID"]
    assert client.get("/_mock/evses").json()[0]["status"] == "Occupied"

    stop = client.post(STOP, json={"SessionID": session, "ProviderID": "TRTST", "EvseID": "TR*ZES*E10001"})
    assert stop.json()["Result"] is True
    assert client.get("/_mock/evses").json()[0]["status"] == "Available"

    cdrs = client.post(CDRS, json=WIDE).json()
    assert cdrs["totalElements"] == 1
    cdr = cdrs["content"][0]
    assert cdr["SessionID"] == session and cdr["EvseID"] == "TR*ZES*E10001" and cdr["ConsumedEnergy"] == 10.0
    assert cdr["ChargingStart"] <= cdr["ChargingEnd"]


@pytest.mark.parametrize(
    "setup, evse, want",
    [
        (None, "TR*ZES*E99999", "603"),
        (None, "TR*ZES*E10002", "604"),
        (None, "TR*ZES*E10003", "700"),
    ],
)
def test_remote_start_refusals(client, setup, evse, want):
    r = start(client, evse)
    assert r.json()["Result"] is False and code(r) == want


def test_second_start_on_a_busy_evse_is_602(client):
    start(client)
    assert code(start(client)) == "602"


def test_remote_stop_of_an_unknown_or_ended_session_is_400(client):
    body = {"SessionID": "b2688855-7f00-0002-6d8e-48d883f6abb6", "ProviderID": "TRTST", "EvseID": "TR*ZES*E10001"}
    assert code(client.post(STOP, json=body)) == "400"
    sid = start(client).json()["SessionID"]
    body["SessionID"] = sid
    client.post(STOP, json=body)
    assert code(client.post(STOP, json=body)) == "400"


def test_cdr_pull_filters_by_time_and_pages(client):
    for _ in range(3):
        sid = start(client).json()["SessionID"]
        client.post(STOP, json={"SessionID": sid, "ProviderID": "TRTST", "EvseID": "TR*ZES*E10001"})
    page = client.post(CDRS + "?size=2&page=1", json=WIDE).json()
    assert (page["totalElements"], page["numberOfElements"], page["totalPages"], page["last"]) == (3, 1, 2, True)
    assert client.post(CDRS, json={**WIDE, "To": "2000-01-01T00:00:00.000Z"}).json()["totalElements"] == 0
    assert client.post(CDRS + "?size=99999", json=WIDE).json()["size"] == 2000


def test_evse_data_and_status_reflect_the_catalogue(app, client):
    data = client.post(
        "/evsepull/v23/providers/TRTST/data-records",
        json={"ProviderID": "TRTST", "GeoCoordinatesResponseFormat": "Google"},
    ).json()
    by_id = {r["EvseID"]: r for r in data["content"]}
    assert (
        by_id["TR*ZES*E10002"]["IsHubjectCompatible"] is False and by_id["TR*ZES*E10001"]["IsHubjectCompatible"] is True
    )
    start(client)
    status = client.post("/evsepull/v21/providers/TRTST/status-records", json={"ProviderID": "TRTST"}).json()
    seen = {
        r["EvseID"]: r["EvseStatus"]
        for o in status["EvseStatuses"]["OperatorEvseStatus"]
        for r in o["EvseStatusRecord"]
    }
    assert seen == {
        "TR*ZES*E10001": "Occupied",
        "TR*ZES*E10002": "Available",
        "TR*ZES*E10003": "OutOfService",
        "TR*TRG*E20001": "Available",
    }


def test_reservation_blocks_a_second_reservation(client):
    body = {"ProviderID": "TRTST", "EvseID": "TR*ZES*E10001", "Identification": EVCO}
    path = "/reservation/v11/providers/TRTST/reservation-start-request"
    assert client.post(path, json=body).json()["Result"] is True
    assert code(client.post(path, json=body)) == "601"


def test_catalogue_can_be_changed_through_the_admin_api(client):
    client.put("/_mock/evses/TR*ZES*E10001", json={"out_of_service": True})
    assert code(start(client)) == "700"
    client.put("/_mock/evses/TR*NEW*E30001", json={"operator_id": "TR*NEW"})
    assert start(client, "TR*NEW*E30001").json()["Result"] is True


# ---- contract quirks and input checking -----------------------------------------------------------------------


def test_remote_start_without_session_id_is_accepted_although_the_contract_requires_it(app, client):
    schema_required = app.state.oicp.ops[2].request_schema["required"]
    assert "SessionID" in schema_required  # the upstream contract; see IGNORE_REQUIRED in oicp.py
    assert start(client).status_code == 200


def test_other_missing_required_fields_are_a_022_with_http_400(client):
    r = client.post(START, json={"ProviderID": "TRTST", "Identification": EVCO})
    assert r.status_code == 400 and code(r) == "022" and "EvseID" in r.json()["StatusCode"]["AdditionalInfo"]


def test_a_body_that_is_not_json_is_a_022_with_http_400(client):
    r = client.post(START, content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 400 and code(r) == "022"


# ---- failure injection ---------------------------------------------------------------------------------------


def rule(client, **spec):
    r = client.post("/_mock/rules", json=spec)
    assert r.status_code == 201, r.text
    return r.json()


def test_every_status_code_can_be_injected_on_every_served_operation(app, client):
    oicp = app.state.oicp
    checked = 0
    for op in oicp.served():
        for status_code in oicp.codes:
            client.delete("/_mock/rules")
            rule(client, operation=op.key, do={"status_code": status_code}, times=1)
            resp = client.post(path_of(op), json=sample_request(oicp, op))
            assert resp.status_code == 200, (op.key, status_code)
            body = resp.json()
            assert body["StatusCode"]["Code"] == status_code
            assert body["StatusCode"]["Description"] == oicp.codes[status_code]
            if "Result" in body:
                assert body["Result"] is False
            assert not schema.errors(oicp._validators[op.key][1], body), (op.key, status_code)
            checked += 1
    assert checked == 9 * 31


def test_injected_description_and_additional_info_are_used(client):
    rule(
        client,
        operation="remote-start",
        do={"status_code": "021", "description": "System error", "additional_info": "CPO backend down"},
        times=1,
    )
    s = start(client).json()["StatusCode"]
    assert s == {"Code": "021", "Description": "System error", "AdditionalInfo": "CPO backend down"}


def test_a_rule_can_match_on_the_body_and_runs_out(client):
    rule(client, operation="remote-start", when={"EvseID": "TR*ZES*E10001"}, do={"status_code": "320"}, times=2)
    assert code(start(client)) == "320"
    assert code(start(client, "TR*TRG*E20001")) == "000"  # other EVSE: untouched
    assert code(start(client)) == "320"
    assert code(start(client)) == "000"  # hits used up
    hits = client.get("/_mock/rules").json()[0]["hits"]
    assert hits == 2


def test_a_rule_can_match_on_the_path(client):
    rule(client, operation="remote-start", when={"path.providerID": "TRXXX"}, do={"status_code": "310"})
    assert code(start(client)) == "000"
    r = client.post(
        "/charging/v21/providers/TRXXX/authorize-remote/start",
        json={"ProviderID": "TRTST", "EvseID": "TR*ZES*E10001", "Identification": EVCO},
    )
    assert code(r) == "310"


def test_http_failures_and_rate_limit_rules(client):
    rule(client, operation="evse-data-pull", do={"http_status": 503}, times=1)
    path = "/evsepull/v23/providers/TRTST/data-records"
    body = {"ProviderID": "TRTST", "GeoCoordinatesResponseFormat": "Google"}
    assert client.post(path, json=body).status_code == 503
    assert client.post(path, json=body).status_code == 200
    rule(client, operation="evse-data-pull", do={"rate_limit": {"retry_after_s": 30}}, times=1)
    r = client.post(path, json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "30"


def test_delay_slows_the_answer_but_keeps_it(client):
    import time

    rule(client, operation="remote-start", do={"delay_ms": 150}, times=1)
    t = time.monotonic()
    assert start(client).json()["Result"] is True
    assert time.monotonic() - t >= 0.14


@pytest.mark.parametrize(
    "spec",
    [
        {"do": {}},
        {"do": {"status_code": "603", "http_status": 500}},
        {"do": {"nonsense": 1}},
        {"operation": "no-such-op", "do": {"status_code": "603"}},
        {"do": {"status_code": "603"}, "times": 0},
    ],
)
def test_bad_rules_are_refused(client, spec):
    assert client.post("/_mock/rules", json=spec).status_code == 422


def test_request_log_reset_and_rule_removal(client):
    r = rule(client, operation="remote-start", do={"status_code": "320"})
    start(client)
    log = client.get("/_mock/requests?operation=remote-start").json()
    assert len(log) == 1 and log[0]["rule"] == r["id"] and log[0]["status"] == 200
    assert client.delete(f"/_mock/rules/{r['id']}").status_code == 204
    assert client.delete(f"/_mock/rules/{r['id']}").status_code == 404
    client.post("/_mock/reset")
    assert client.get("/_mock/requests").json() == [] and client.get("/_mock/sessions").json() == []


# ---- Acceptable Use Policy ----------------------------------------------------------------------------------


def test_aup_limit_is_enforced_per_minute_when_switched_on():
    now = [1_000_000.0]
    app = create_app(enforce_aup=True, time_source=lambda: now[0])
    app.state.oicp.limits["AuthorizeRemoteStart"] = 2
    with TestClient(app) as c:
        statuses = [start(c, "TR*TRG*E20001").status_code for _ in range(3)]
        assert statuses == [200, 200, 429]  # the second start is 602 but still HTTP 200
        retry = start(c).headers["retry-after"]
        assert 1 <= int(retry) <= 61
        now[0] += 60
        assert start(c).status_code == 200


def test_aup_is_off_by_default(client):
    assert all(start(client, "TR*TRG*E20001").status_code == 200 for _ in range(150))


def test_clock_is_injectable_for_cdr_times():
    fixed = datetime(2026, 11, 2, 9, 0, 0, tzinfo=UTC)
    with TestClient(create_app(clock=lambda: fixed)) as c:
        sid = start(c).json()["SessionID"]
        c.post(STOP, json={"SessionID": sid, "ProviderID": "TRTST", "EvseID": "TR*ZES*E10001"})
        cdr = c.post(CDRS, json=WIDE).json()["content"][0]
        assert cdr["SessionStart"] == "2026-11-02T09:00:00.000Z" == cdr["SessionEnd"]
