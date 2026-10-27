"""Find and load the pinned hub contracts and profiles (EV_PNC_hub-contracts).

The simulator never carries its own copy. `contracts.lock` names the hub-contracts commit it was
tested against; `make contracts` fetches it into `.contracts/`. Point EV_PNC_CONTRACTS_DIR at any
other checkout to try a newer pin.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent


class ContractsMissing(RuntimeError):
    pass


def contracts_dir() -> Path:
    env = os.environ.get("EV_PNC_CONTRACTS_DIR")
    path = Path(env) if env else ROOT / ".contracts"
    if not (path / "_sources").is_dir():
        raise ContractsMissing(f"no hub-contracts checkout at {path}; run `make contracts` or set EV_PNC_CONTRACTS_DIR")
    return path


@cache
def _load(path: Path) -> Any:
    text = path.read_text()
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def source(name: str) -> dict:
    """A generated contract in _sources/, for example hubject-oicp-2.3-emp.openapi.yaml."""
    return _load(contracts_dir() / "_sources" / name)


def profile(name: str) -> dict:
    """A hub profile in profiles/."""
    return _load(contracts_dir() / "profiles" / name)


def sha256(name: str) -> str:
    """Hash of a _sources file, taken from SHA256SUMS, so a mock can report what it serves."""
    for line in (contracts_dir() / "_sources" / "SHA256SUMS").read_text().splitlines():
        digest, _, fname = line.partition("  ")
        if fname.strip() == name:
            return digest
    raise KeyError(name)
