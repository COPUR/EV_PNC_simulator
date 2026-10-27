"""Hand-written OCPI 2.2.1 sample data served by the GIREVE mock (synthetic, not taken from any spec text).

Shapes follow what the GIREVE profile says an eMSP receives: owner, operator, connector tariff ids and EVSE
capabilities are always present, `gireve_id` is the vendor extension, tariffs are type REGULAR.
"""

from __future__ import annotations

HUB_COUNTRY, HUB_PARTY = "FR", "107"  # pre-production IOP party, from the profile
CPO_COUNTRY, CPO_PARTY = "FR", "ABC"
EMSP_COUNTRY, EMSP_PARTY = "TR", "TST"
DEFAULT_UID = "04AABBCCDDEEFF"  # synthetic 7-byte RFID uid; low entropy on purpose so secret scanners leave it alone


def location(n: int, updated: str) -> dict:
    return {
        "country_code": CPO_COUNTRY,
        "party_id": CPO_PARTY,
        "id": f"LOC{n:04d}",
        "publish": True,
        "name": f"Mock station {n}",
        "address": f"{n} rue de l'Exemple",
        "city": "Lyon",
        "postal_code": "69001",
        "country": "FRA",
        "coordinates": {"latitude": "45.760000", "longitude": "4.840000"},
        "owner": {"name": "Mock Owner SAS"},
        "operator": {"name": "Mock Operator SAS"},
        "time_zone": "Europe/Paris",
        "evses": [
            {
                "uid": f"FR*ABC*E{n}0001",
                "evse_id": f"FR*ABC*E{n}0001",
                "status": "AVAILABLE",
                "capabilities": ["ISO_15118_2_PLUG_AND_CHARGE", "RFID_READER"],
                "connectors": [
                    {
                        "id": "1",
                        "standard": "IEC_62196_T2_COMBO",
                        "format": "CABLE",
                        "power_type": "DC",
                        "max_voltage": 400,
                        "max_amperage": 125,
                        "tariff_ids": [f"TAR{n:03d}"],
                        "last_updated": updated,
                    }
                ],
                "last_updated": updated,
            }
        ],
        "gireve_id": f"gir-{n}",
        "last_updated": updated,
    }


def tariff(n: int, updated: str) -> dict:
    return {
        "country_code": CPO_COUNTRY,
        "party_id": CPO_PARTY,
        "id": f"TAR{n:03d}",
        "currency": "EUR",
        "type": "REGULAR",
        "elements": [{"price_components": [{"type": "ENERGY", "price": 0.39, "vat": 20.0, "step_size": 1}]}],
        "gireve_id": f"gir-tar-{n}",
        "last_updated": updated,
    }


def locations(count: int = 3) -> list[dict]:
    return [location(n, f"2026-10-{20 + n:02d}T08:00:00Z") for n in range(1, count + 1)]


def tariffs(count: int = 3) -> list[dict]:
    return [tariff(n, f"2026-10-{20 + n:02d}T08:00:00Z") for n in range(1, count + 1)]


def cdr_token(uid: str = "04A1B2C3D4E5F6") -> dict:
    return {
        "country_code": EMSP_COUNTRY,
        "party_id": EMSP_PARTY,
        "uid": uid,
        "type": "RFID",
        "contract_id": "TR-TST-C00000001-1",
    }


def session(session_id: str, updated: str = "2026-10-27T10:00:00Z") -> dict:
    return {
        "country_code": CPO_COUNTRY,
        "party_id": CPO_PARTY,
        "id": session_id,
        "start_date_time": "2026-10-27T09:00:00Z",
        "kwh": 12.5,
        "cdr_token": cdr_token(),
        "auth_method": "WHITELIST",
        "location_id": "LOC0001",
        "evse_uid": "FR*ABC*E10001",
        "connector_id": "1",
        "currency": "EUR",
        "status": "ACTIVE",
        "last_updated": updated,
    }


def cdr(cdr_id: str, session_id: str, remark: str = "[*] priced by the hub") -> dict:
    """2.2.1 shape: prices are {excl_vat, incl_vat}, which the canonical 2.3.0 model spells before_taxes and taxes."""
    return {
        "country_code": CPO_COUNTRY,
        "party_id": CPO_PARTY,
        "id": cdr_id,
        "session_id": session_id,
        "start_date_time": "2026-10-27T09:00:00Z",
        "end_date_time": "2026-10-27T09:45:00Z",
        "cdr_token": cdr_token(),
        "auth_method": "WHITELIST",
        "cdr_location": {
            "id": "LOC0001",
            "address": "1 rue de l'Exemple",
            "city": "Lyon",
            "country": "FRA",
            "coordinates": {"latitude": "45.760000", "longitude": "4.840000"},
            "evse_uid": "FR*ABC*E10001",
            "evse_id": "FR*ABC*E10001",
            "connector_id": "1",
            "connector_standard": "IEC_62196_T2_COMBO",
            "connector_format": "CABLE",
            "connector_power_type": "DC",
        },
        "currency": "EUR",
        "charging_periods": [
            {"start_date_time": "2026-10-27T09:00:00Z", "dimensions": [{"type": "ENERGY", "volume": 12.5}]}
        ],
        "total_cost": {"excl_vat": 4.88, "incl_vat": 5.85},
        "total_energy": 12.5,
        "total_time": 0.75,
        "remark": remark,
        "last_updated": "2026-10-27T09:46:00Z",
    }
