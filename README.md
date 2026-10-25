# EV_PNC_simulator

Simulators and end-to-end harness: OCPI CPO, ISO 15118 EV/charger, hub mocks generated from EV_PNC_hub-contracts pins, failure injection, corridor trip replays. Consumed as container images.

Part of the **EV_PNC** platform: a neutral eMSP integration layer for Turkey-first, Europe-ready Plug&Charge, navigation and payments. The platform is neither a CPO nor a payment institution.

> Plug in, charging starts, payment completes in the background.

## Scope

- CPO simulator OCPI 2.2.1 / 2.3.0; ISO 15118-2 EVCC/SECC (e.g. Josev)
- Hub mocks: Hubject OICP/OPCP, GIREVE IOP/PNCP, ECHX (with 5xx, timeouts, partial pages)
- Corridor scenarios M, A1 to A4 and peak-day load
- End-to-end acceptance harness for @mock scenarios
- Rule: other repos use it as an image over the wire, never as a library

## Tech stack

Python, OCPI mock server, ISO 15118 EVCC/SECC

## Packaging

- Published components: simulator
- Depends on: EV_PNC_hub-contracts, EV_PNC_ocpi-model
- Depended on by: no repo
- Dependency rules: no cycles (ADP); depend towards more stable components (SDP); published `*-api` components hold abstractions, deployables hold concrete code (SAP). CI runs `tools/packaging_check.py --strict` from `EV_PNC_docs`.
- Owner: QA agent · Wave 1 (now, before the 2026-12-07 gate) · Visibility: public

## Proposed layout

```
simulator/{cpo,ev,hub}
scenarios/{corridor,peak}
```

## Related

- Plan: `repo-plan/v2/README.md` (project files); consent repos: `EV_PnC_Consent_*` (`repo-plan/consent/README.md`)
- Docs: [EV_PNC_docs](https://github.com/COPUR/EV_PNC_docs)

## Status

Scaffold only. Not production ready.

## License

Apache-2.0 (proposed)
