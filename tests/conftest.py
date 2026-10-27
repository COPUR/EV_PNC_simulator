import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if "EV_PNC_CONTRACTS_DIR" not in os.environ and not (ROOT / ".contracts").is_dir():
    pytest.exit(
        "no contracts: run `make contracts` or set EV_PNC_CONTRACTS_DIR to an EV_PNC_hub-contracts checkout",
        returncode=2,
    )
