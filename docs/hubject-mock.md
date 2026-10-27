# Hubject mock hub (H-03)

A mock of Hubject's hub for the eMSP role, built from the contracts pinned in
[EV_PNC_hub-contracts](https://github.com/COPUR/EV_PNC_hub-contracts). It has no copy of any contract:
`contracts.lock` names the hub-contracts commit it was tested against, and the container is built from that
checkout. When the pins move, the tests say what broke.

```bash
make contracts                       # fetch the pinned contracts into .contracts/
make test                            # 60 tests (Hubject), more for GIREVE
uv run python -m ev_pnc_simulator.hubject --port 8080 [--enforce-aup] [--no-auth]
```

Status: **assumption** that the mock is Python and Starlette. The cards fix no language for the simulator;
reverse it with an ADR if the squad prefers Kotlin (the mock holds no business logic, only the contract).

## What it covers

**OICP 2.3 (EMP role).** All 14 operations of the pinned contract, split by who sends the request:

| Group | Operations (rule and drive key) | How |
|---|---|---|
| Served: the eMSP calls Hubject | `remote-start`, `remote-stop`, `cdr-pull`, `auth-data-push`, `reservation-start`, `evse-data-pull`, `evse-status-pull`, `pricing-products-pull`, `evse-pricing-pull` | HTTP routes at the contract's paths |
| Driven: Hubject calls the eMSP | `authorize-start`, `authorize-stop`, `cdr-push`, `reservation-stop`, `charging-notifications` | `POST /_mock/drive/<key>` with `{"target": "http://emsp"}` |

Every request is validated against the pinned request schema, every driven request is validated before it is
sent, and the eMSP's answer is validated against the response schema (`response_valid`).

**OPCP 1.0.** All 22 operations of the six eMSP-role contracts (CCP, CPS, EST, PCP, RCP, webhooks) at their
paths. They answer with a minimal valid sample of their first 2xx response and need a bearer token (401 without
one; `--no-auth` turns that off). The webhook endpoint API is stateful. Signed events are sent with
`POST /_mock/webhooks/<endpointId>/emit`.

**Default behaviour** (so a client can run a whole flow without any rule): a catalogue of four synthetic EVSEs
(`TR*ZES*E10001` normal, `E10002` not Hubject-compatible, `E10003` out of service, `TR*TRG*E20001` normal).
Remote start answers 603, 604, 700, 602 or 601 as the flow document describes (`roaming-hubs/runs/hubject/flows.md` §4);
a stopped session produces a CDR that `cdr-pull` returns, paged (`?page=`, `?size=`, at most 2000). Remote
stop of an unknown or ended session is 400. The EVSE catalogue is editable at `PUT /_mock/evses/<id>`.

## Failure injection (T-HUBJECT-33)

```bash
curl -XPOST :8080/_mock/rules -d '{"operation":"remote-start","when":{"EvseID":"TR*ZES*E10001"},
                                    "do":{"status_code":"320"},"times":2}'
```

| `do` | Effect |
|---|---|
| `{"status_code": "603"}` | OICP business failure: HTTP 200, `Result` false (or `AuthorizationStatus` NotAuthorized), StatusCode with the contract's description. All 31 codes, on every served operation (279 combinations in the tests). |
| `{"http_status": 503}` | A real HTTP failure. On OPCP the error body is `{"errorMessages": [...], "traceId": "..."}`. Every code each OPCP operation declares can be injected (EST's `5XX` takes 500, 502, 503). |
| `{"rate_limit": {"retry_after_s": 30}}` | HTTP 429 with `Retry-After`. |
| `{"delay_ms": 12000}` | Wait, then answer normally (use it above a client's timeout to simulate a hang). |

`when` matches values in the JSON body by dotted path (`Identification.RemoteIdentification.EvcoID`) or in the
path (`path.providerID`). A rule with `times` runs out. The first matching rule wins. Admin API: `GET/POST/DELETE
/_mock/rules`, `GET/DELETE /_mock/requests` (logs every call, credentials masked), `POST /_mock/reset`,
`GET /_mock/health` (lists the contract file hashes it serves), `GET /_mock/status-codes`.

`--enforce-aup` applies the Acceptable Use Policy limits of the profile (calls per minute per service) and
answers 429 above them. The profile records a conflict for PullEVSEData (100 in the policy, 25 on a support
page); the mock uses the policy value.

## Findings from the contract, for the squad

1. **Remote start requires `SessionID` in the pinned schema, but Hubject assigns it** (spec prose, profile quirk
   `remote-start-no-sessionid`). The mock does not insist on it. A client generated from the schema would send a
   made-up id.
2. The upstream schemas use `maximum` on strings where `maxLength` is meant (StatusCode `Description` 200,
   `AdditionalInfo` 1000). Validators ignore it, so the mock does not enforce those limits; it truncates its own
   output to them.
3. The response of `status-records` is a `oneOf`: the status list (`EvseStatuses`) or the by-ID answer (`EVSEStatusRecords`) of a different operation. The mock returns the first. `status-records-by-operator-id` and the by-ID pull are not in the pinned subset (profile `paths_extra`), so they are not mocked.
4. The webhook event names differ between the registration API (`mo.prov.cert.updated.factory.reset`) and the
   AsyncAPI profile (`mo.prov.cert.factory.reset`). The mock accepts both when emitting, and only the API's names
   when registering. Ask Hubject (U).
5. The OICP signature header has three names in the sources (`X-Hubject-Signature`, `X-Operator-Signature`,
   `payload-signature`). The mock can send any; verify whichever is present.
6. OPCP error bodies point at `cps.v1.json`, a file outside the pinned subset. The mock uses the shape
   `{errorMessages, traceId}` that `Error` has there.

## Not covered

- TLS and client certificates (Hubject authenticates by certificate and IP allow-list). The mock speaks plain HTTP.
- A dropped connection (not possible under ASGI); delay is the substitute.
- Webhook retries (3, one hour apart, on 5xx). The delivery result is returned, so a test can retry itself.
- Contract behaviour the spec does not state (U in the flow document): which StatusCode an eMSP must use for a
  refusal, what the hub does when an eMSP answers after 10 seconds.
