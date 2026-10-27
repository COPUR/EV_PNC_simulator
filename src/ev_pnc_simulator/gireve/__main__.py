"""Run the GIREVE mock: python -m ev_pnc_simulator.gireve --port 8081 --public-url http://localhost:8081"""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--public-url", help="base URL the eMSP reaches this mock at (default http://<host>:<port>)")
    ap.add_argument(
        "--no-auto-callbacks", action="store_true", help="hold command results until POST /_mock/commands/<uid>/deliver"
    )
    args = ap.parse_args()
    url = args.public_url or f"http://{args.host}:{args.port}"
    uvicorn.run(create_app(base_url=url, auto_callbacks=not args.no_auto_callbacks), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
