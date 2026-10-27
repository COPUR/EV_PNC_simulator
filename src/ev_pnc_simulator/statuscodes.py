"""The OICP 2.3 StatusCode list, read from the pinned EMP contract (never typed in here)."""

from __future__ import annotations

import re

ROW = re.compile(r"\|\s*(\d{3})\s*\|\s*([^|]+?)\s*\|")


def oicp_status_codes(emp_doc: dict) -> dict[str, str]:
    """Code -> description, parsed from the table in the StatusCode.Code description of an acknowledgement."""
    for item in emp_doc["paths"].values():
        schema = item["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        status = schema.get("properties", {}).get("StatusCode")
        if status and "Code" in status.get("properties", {}):
            table = status["properties"]["Code"].get("description", "")
            codes = {m[1]: m[2].rstrip(".").strip() for m in ROW.finditer(table)}
            if codes:
                return codes
    raise ValueError("no StatusCode table found in the contract")


def success_status(codes: dict[str, str]) -> dict:
    return {"Code": "000", "Description": codes.get("000", "Success"), "AdditionalInfo": "Success"}
