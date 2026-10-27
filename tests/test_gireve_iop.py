"""GIREVE mock as the IOP: what an eMSP calls on it. Expectations come from the pinned GIREVE profile."""

from __future__ import annotations

import pytest
from gireve_support import FakeEmsp, V, as_emsp, b64, iop_token, mock_for, push_token, register, rfid_token

from ev_pnc_simulator import contracts


@pytest.fixture
def emsp():
    return FakeEmsp()


@pytest.fixture
def c(emsp):
    with mock_for(emsp) as client:
        assert register(client)["registered"]
        yield client


def test_register_from_iop_walks_versions_details_then_posts_credentials(emsp):
    with mock_for(emsp) as c:
        out = register(c)
        assert out["registered"], out
        assert [s["step"] for s in out["steps"]] == ["GET versions", "GET version details", "POST credentials"]
        assert all(s["ok"] for s in out["steps"])
        hub = c.app.state.hub
        assert hub.reg.emsp_token == "EMSP-FINAL-TOKEN" and hub.reg.emsp_party == ("TR", "TST")
    # the eMSP got the IOP's credentials: its token, the versions URL and a HUB role
    creds = emsp.hub_credentials
    assert creds["roles"][0]["role"] == "HUB" and creds["url"].endswith("/ocpi/versions")
    assert [s["headers"]["authorization"] for s in emsp.seen[:2]] == [b64("TEMP-TOKEN-A")] * 2


def test_registration_stops_when_the_emsp_does_not_offer_2_2_1():
    with mock_for(FakeEmsp(versions=("2.1.1",))) as c:
        out = register(c)
    assert not out["registered"] and len(out["steps"]) == 1 and not out["steps"][0]["ok"]


def test_registration_needs_both_urls(emsp):
    with mock_for(emsp) as c:
        assert c.post("/_mock/drive/register", json={}).status_code == 422


def test_calls_before_registration_are_refused(emsp):
    with mock_for(emsp) as c:
        r = c.get("/ocpi/versions", headers={"Authorization": b64("anything")})
    assert r.status_code == 401 and r.json()["status_code"] == 2000


def test_a_token_that_is_not_base64_is_refused(c):
    r = c.get("/ocpi/versions", headers={"Authorization": f"Token {iop_token(c)}-"})
    assert r.status_code == 401 and "Base64" in r.json()["status_message"]
    assert c.get("/ocpi/versions", headers={"Authorization": b64("wrong")}).status_code == 401


def test_versions_and_details_advertise_only_what_the_profile_supports(c):
    h = {"Authorization": b64(iop_token(c))}
    assert c.get("/ocpi/versions", headers=h).json()["data"] == [{"version": "2.2.1", "url": "http://hub/ocpi/2.2.1"}]
    ep = {e["identifier"]: e["role"] for e in c.get(V, headers=h).json()["data"]["endpoints"]}
    assert ep == {
        "credentials": "SENDER",
        "locations": "SENDER",
        "tariffs": "SENDER",
        "tokens": "RECEIVER",
        "commands": "RECEIVER",
    }
    assert "chargingprofiles" not in ep


def test_credentials_and_versions_do_not_need_routing_headers_but_modules_do(c):
    h = {"Authorization": b64(iop_token(c))}
    assert c.get(f"{V}/credentials", headers=h).json()["status_code"] == 1000
    r = c.get(f"{V}/locations", headers=h).json()
    assert r["status_code"] == 2001 and "ocpi-to-country-code" in r["status_message"]


def test_credentials_update_is_not_implemented(c):
    r = c.put(f"{V}/credentials", json={}, headers={"Authorization": b64(iop_token(c))})
    assert r.status_code == 405 and "unregister" in r.json()["status_message"]


def test_tracing_ids_are_echoed(c):
    r = c.get(f"{V}/locations", headers=as_emsp(c))
    assert r.headers["x-request-id"] == "req-1" and r.headers["x-correlation-id"] == "cor-1"


def test_locations_are_paged_with_link_and_total(c):
    r = c.get(f"{V}/locations", params={"limit": 2}, headers=as_emsp(c))
    assert len(r.json()["data"]) == 2
    assert r.headers["x-total-count"] == "3" and r.headers["x-limit"] == "2"
    assert 'rel="next"' in r.headers["link"] and "offset=2" in r.headers["link"]
    last = c.get(f"{V}/locations", params={"limit": 2, "offset": 2}, headers=as_emsp(c))
    assert len(last.json()["data"]) == 1 and "link" not in last.headers


def test_page_size_is_capped_at_the_profile_limit(c):
    r = c.get(f"{V}/locations", params={"limit": 5000}, headers=as_emsp(c))
    assert (
        r.headers["x-limit"]
        == str(contracts.profile("gireve-ocpi-2.2.1.profile.yaml")["tariffs"]["pull_limits"]["max_per_page"])
        == "100"
    )


def test_locations_carry_the_fields_gireve_makes_mandatory(c):
    for loc in c.get(f"{V}/locations", headers=as_emsp(c)).json()["data"]:
        assert loc["owner"]["name"] and loc["operator"]["name"] and loc["gireve_id"]
        for evse in loc["evses"]:
            assert evse["capabilities"] and all(con["tariff_ids"] for con in evse["connectors"])


def test_date_from_filters_on_last_updated(c):
    r = c.get(f"{V}/locations", params={"date_from": "2026-10-22T00:00:00Z"}, headers=as_emsp(c))
    assert [loc["id"] for loc in r.json()["data"]] == ["LOC0002", "LOC0003"]
    assert c.get(f"{V}/locations", params={"date_from": "yesterday"}, headers=as_emsp(c)).json()["status_code"] == 2001


def test_tariff_pull_is_limited_to_one_month(c):
    h = as_emsp(c)
    ok = c.get(f"{V}/tariffs", params={"date_from": "2026-10-01T00:00:00Z"}, headers=h).json()
    assert ok["status_code"] == 1000
    too_long = c.get(f"{V}/tariffs", params={"date_from": "2026-01-01T00:00:00Z"}, headers=h).json()
    assert too_long["status_code"] == 2001 and "range" in too_long["status_message"]
    # locations have no such limit
    assert (
        c.get(f"{V}/locations", params={"date_from": "2026-01-01T00:00:00Z"}, headers=h).json()["status_code"] == 1000
    )


def test_sessions_and_cdrs_pull_and_token_get_are_not_implemented(c):
    h = as_emsp(c)
    for path in ("sessions", "cdrs", "tokens/TR/TST/04A1B2C3D4E5F6"):
        assert c.get(f"{V}/{path}", headers=h).status_code == 405, path
    assert c.patch(f"{V}/tokens/TR/TST/04A1B2C3D4E5F6", json={}, headers=h).status_code == 405


def test_token_put_creates_then_replaces(c):
    first, second = push_token(c), push_token(c)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["status_code"] == 1000
    assert list(c.app.state.hub.tokens) == [("TR", "TST", "04A1B2C3D4E5F6")]


@pytest.mark.parametrize(
    "uid, why",
    [
        ("04a1b2c3d4e5f6", "upper-case"),
        ("04A1B2C3D4", "4 or 7 bytes"),
        ("04A1B2C", "4 or 7 bytes"),
        ("04A1B2C3D", "4 or 7 bytes"),
    ],
)
def test_rfid_uid_format_is_enforced(c, uid, why):
    out = push_token(c, rfid_token(uid)).json()
    assert out["status_code"] == 2001 and why in out["status_message"]


def test_four_and_seven_byte_uids_are_accepted(c):
    assert push_token(c, rfid_token("04A1B2C3")).json()["status_code"] == 1000
    assert push_token(c, rfid_token("04A1B2C3D4E5F6")).json()["status_code"] == 1000


def test_token_must_match_its_url_and_be_complete(c):
    t = rfid_token()
    wrong_url = c.put(f"{V}/tokens/TR/TST/04A1B2C3", json=t, headers=as_emsp(c)).json()
    assert wrong_url["status_code"] == 2001
    partial = {k: v for k, v in t.items() if k not in ("issuer", "whitelist")}
    out = push_token(c, partial).json()
    assert out["status_code"] == 2001 and "issuer" in out["status_message"] and "whitelist" in out["status_message"]


def test_plug_and_charge_tokens_are_not_pushed(c):
    out = push_token(c, rfid_token("TR*TST*C00000001*1", type="OTHER")).json()
    assert out["status_code"] == 2001 and "Plug&Charge" in out["status_message"]


def test_token_put_requires_a_json_body(c):
    r = c.put(f"{V}/tokens/TR/TST/04A1B2C3", content=b"{nope", headers=as_emsp(c))
    assert r.status_code == 400
