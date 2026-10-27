"""Run the Hubject mock: python -m ev_pnc_simulator.hubject --port 8080"""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument(
        "--enforce-aup", action="store_true", help="answer 429 above the Acceptable Use Policy limits in the profile"
    )
    ap.add_argument("--no-auth", action="store_true", help="do not require a bearer token on OPCP calls")
    args = ap.parse_args()
    uvicorn.run(create_app(enforce_aup=args.enforce_aup, require_auth=not args.no_auth), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
