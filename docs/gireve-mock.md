# GIREVE mock hub (H-04)

A mock of GIREVE as an eMSP sees it: the IOP (OCPI 2.2.1) and the PNCP webhooks. It follows the pinned profiles
in [EV_PNC_hub-contracts](https://github.com/COPUR/EV_PNC_hub-contracts) (`gireve-ocpi-2.2.1.profile.yaml`,
`gireve-pncp-events.asyncapi.yaml`) and has no logic of its own: the rules come from the profile, the profile
comes from the specs (ADR-0001, "hub rules as data"). `contracts.lock` names the commit it was tested against.

```bash
make contracts                       # fetch the pinned contracts into .contracts/
make test                            # all mock tests
uv run python -m ev_pnc_simulator.gireve --port 8081 --public-url http://localhost:8081 [--no-auto-callbacks]
```

Status: **assumption** that the mock is Python and Starlette, as for the Hubject mock.

## What it covers

Unlike Hubject, GIREVE is plain OCPI, so the mock is built around OCPI roles: the eMSP calls the IOP (served), and
the IOP calls the eMSP (driven, `POST /_mock/drive/<key>`).

| Group | Operations (rule and drive key) | Behaviour taken from the profile |
|---|---|---|
| Served: `/ocpi/versions`, `/ocpi/2.2.1` | `versions`, `version-details` | Only modules GIREVE supports are advertised (no `chargingprofiles`, no `sessions`/`cdrs` for an eMSP to pull). |
| Served: credentials | `credentials-get`, `credentials-write` | Registration is started by the IOP. A credentials update (POST, PUT, DELETE) is answered 405: unregister, then register again. |
| Served: pulls | `locations-get`, `tariffs-get` | Pages of at most 100 (`X-Total-Count`, `X-Limit`, `Link`); `date_from`/`date_to` on `last_updated`; tariffs refuse a range over one month (2001). Locations carry owner, operator, connector tariff ids, EVSE capabilities and `gireve_id`. |
| Served: not implemented | `sessions-get`, `cdrs-get`, `token-other` (GET, PATCH) | HTTP 405. The profile says the sessions pull is not implemented and the CDR pull is unclear (assumed not implemented). |
| Served: tokens | `token-put` | Full PUT only; RFID uid in upper-case hex of 4 or 7 bytes; all required fields; body must equal the URL; Plug&Charge (eMAID) tokens refused, they are authorized in real time. 201 on create, 200 on replace. |
| Served: commands | `command` | START_SESSION (needs `evse_uid`, a pushed token, a known location) and STOP_SESSION are accepted; RESERVE_NOW, CANCEL_RESERVATION and UNLOCK_CONNECTOR answer `NOT_SUPPORTED`. The CommandResult is POSTed to the eMSP's `response_url` after the synchronous answer. |
| Driven: IOP calls the eMSP | `register`, `unregister`, `authorize`, `location-put`, `tariff-put`, `session-put`, `session-patch`, `cdr-post` | Registration walks versions, details, then POSTs the IOP's credentials and reads the eMSP's token and party. Every call checks HTTP 2xx, status_code 1000 and the echoed tracing ids. |
| PNCP | `POST /_mock/pncp/emit` | Webhook event signed with Base64 HMAC-SHA256 in `payload-signature`. `corrupt_signature` sends a bad one. |

Transport rules enforced: `Authorization: Token <base64>` on every call (a token that is not Base64 is 401),
`OCPI-to-*`/`OCPI-from-*` and `X-Request-ID`/`X-Correlation-ID` on module calls (not on credentials and versions),
tracing ids echoed on every answer. The mock does not model the IP allow-list or the GIREVE-issued client
certificate (plain HTTP).

## Typical flow

```bash
# 1. The IOP registers with your eMSP (your eMSP's versions URL and the one-time token you gave GIREVE)
curl -XPOST :8081/_mock/drive/register -d '{"versions_url":"http://emsp/ocpi/versions","temp_token":"TEMP"}'
# 2. Your eMSP calls the IOP with the token it received in step 1, e.g. pushes a token and starts a session
# 3. Make the CPO side fail the next start
curl -XPOST :8081/_mock/rules -d '{"operation":"command","do":{"command_result":"REJECTED"},"times":1}'
# 4. Push a location, authorize, send a CDR
curl -XPOST :8081/_mock/drive/location-put -d '{}'
curl -XPOST :8081/_mock/drive/authorize    -d '{"token_uid":"04A1B2C3D4E5F6"}'
curl -XPOST :8081/_mock/drive/cdr-post     -d '{"remark":"[?] price not significant"}'
```

## Failure injection (T-GIREVE-01..04, 14, 15)

Same rule engine as the Hubject mock (`operation`, `when`, `do`, `times`; unknown rule keys are rejected).
`when` matches the JSON body by dotted path or the URL by `path.<name>` (`path.token_uid`, `path.command`).

| `do` | Effect |
|---|---|
| `{"ocpi_status": 2003, "message": "..."}` | HTTP 200 with an OCPI envelope carrying that `status_code` (OCPI reports errors in the envelope, not in HTTP). |
| `{"command_result": "REJECTED"}` | The CommandResult the CPO side reports later (`ACCEPTED`, `REJECTED`, `FAILED`, `TIMEOUT`, `CANCELED_RESERVATION`, `UNKNOWN_RESERVATION`). The synchronous answer is unchanged. |
| `{"http_status": 503}`, `{"rate_limit": {...}}`, `{"delay_ms": ...}` | Shared with Hubject. GIREVE's guide names 425 and 429 as "wait" codes. |

`--no-auto-callbacks` holds every CommandResult until `POST /_mock/commands/<uid>/deliver` (optionally with
`{"result": "TIMEOUT"}`), so a test can pick the moment, or never deliver. Admin API as for Hubject, plus
`GET /_mock/tokens`, `/_mock/sessions`, `/_mock/commands`.

## Findings, for the squad

1. **The meaning of Register-FromIOP and ToIOP is not defined in the guide** (Q-GIREVE-01). The mock implements
   FromIOP as "the IOP calls the eMSP's credentials endpoint", which is the reading in the profile. If GIREVE means
   the opposite, `register` flips and the served credentials endpoint becomes a POST.
2. The CDR pull is listed in section 4.1 and declared not implemented in 4.7.5 of the guide. The mock answers 405.
3. The `hubclientinfo` module is contradictory in the guide (table says no, use cases say yes) and is not mocked.
4. The PNCP payload schemas are only partly known upstream (`x-unverified`: field names printed without
   underscores). The mock sends `{timestamp, eventtype, data}` with `data` supplied by the caller; confirm the real
   names on GIREVE's Stoplight site before a test depends on them.
5. A month is 31 days in the mock (lenient). The guide does not say how GIREVE counts it.
6. Sample data is synthetic: location `LOC0001` with EVSE `FR*ABC*E10001`, party `FR*ABC`, IOP party `FR*107`
   (the pre-production party id in the profile).

## Not covered

- TLS, client certificates and the IP allow-list.
- Store-and-forward and the 5/10/20 minute retry of sessions and CDRs (the delivery result is returned, so a test
  can retry itself); the 120 s minimum between meter updates.
- Smart charging and charging preferences (not supported by GIREVE).
- A dropped connection (not possible under ASGI); use `delay_ms` above the client timeout.
