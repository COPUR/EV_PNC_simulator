"""Failure injection: rules that make a mock answer wrongly on purpose.

A rule matches a request by operation and, optionally, by values in the JSON body. The first matching
rule with hits left wins. What a rule does (`do`):

    {"status_code": "603"}                      OICP business failure: HTTP 200, Result false, StatusCode 603
    {"status_code": "603", "description": ..., "additional_info": ...}
    {"http_status": 503}                        a real HTTP failure (OPCP style error body, or any OICP status)
    {"http_status": 500, "body": {...}}         the same with a body of your own
    {"rate_limit": {"retry_after_s": 30}}       HTTP 429 with Retry-After
    {"delay_ms": 12000}                         wait first; alone it only slows the normal answer
    {"ocpi_status": 2003, "message": "..."}     OCPI mocks: HTTP 200 and an OCPI envelope with that status_code
    {"command_result": "REJECTED"}              OCPI mocks: the CommandResult the CPO side reports back later

A dropped connection cannot be produced under ASGI; use a delay above the client's timeout instead.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Any

ACTION_KEYS = {
    "status_code",
    "http_status",
    "rate_limit",
    "delay_ms",
    "description",
    "additional_info",
    "body",
    "headers",
    "ocpi_status",
    "command_result",
    "message",
}
PRIMARY_KEYS = {"status_code", "http_status", "rate_limit", "delay_ms", "ocpi_status", "command_result"}


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    id: str
    operation: str  # an operation key, an operationId, or "*"
    when: dict[str, Any]
    do: dict[str, Any]
    times: int | None = None
    hits: int = 0

    def public(self) -> dict:
        return {
            "id": self.id,
            "operation": self.operation,
            "when": self.when,
            "do": self.do,
            "times": self.times,
            "hits": self.hits,
        }


def dig(body: Any, dotted: str) -> Any:
    """Value at `a.b.c` in nested dicts; list indexes are written as numbers. Missing gives a sentinel."""
    cur = body
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


_MISSING = object()


@dataclass
class RuleSet:
    rules: list[Rule] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    _lock: Any = field(default_factory=threading.Lock)

    def add(self, spec: dict, known_operations: set[str]) -> Rule:
        stray = set(spec) - {"operation", "when", "do", "times"}
        if stray:
            raise RuleError(f"unknown rule keys: {sorted(stray)} (a rule has operation, when, do, times)")
        operation = spec.get("operation", "*")
        if operation != "*" and operation not in known_operations:
            raise RuleError(f"unknown operation {operation!r}")
        do = spec.get("do")
        if not isinstance(do, dict) or not do:
            raise RuleError("`do` must be a non-empty object")
        unknown = set(do) - ACTION_KEYS
        if unknown:
            raise RuleError(f"unknown action keys: {sorted(unknown)}")
        if not PRIMARY_KEYS & set(do):
            raise RuleError(f"`do` needs one of {', '.join(sorted(PRIMARY_KEYS))}")
        if len({"status_code", "http_status", "ocpi_status"} & set(do)) > 1:
            raise RuleError("use one of status_code, http_status, ocpi_status")
        times = spec.get("times")
        if times is not None and (not isinstance(times, int) or times < 1):
            raise RuleError("`times` must be a positive integer")
        with self._lock:
            rule = Rule(f"r{next(self._ids)}", operation, dict(spec.get("when", {})), dict(do), times)
            self.rules.append(rule)
        return rule

    def take(self, keys: set[str], body: Any, context: dict[str, Any] | None = None) -> Rule | None:
        """The first rule that fits and still has hits left; counts the hit."""
        with self._lock:
            for rule in self.rules:
                if rule.times is not None and rule.hits >= rule.times:
                    continue
                if rule.operation != "*" and rule.operation not in keys:
                    continue
                if not all(self._equal(path, want, body, context) for path, want in rule.when.items()):
                    continue
                rule.hits += 1
                return rule
        return None

    @staticmethod
    def _equal(path: str, want: Any, body: Any, context: dict[str, Any] | None) -> bool:
        if path.startswith("path.") and context is not None:
            return context.get(path[5:]) == want
        return dig(body, path) == want

    def remove(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self.rules)
            self.rules = [r for r in self.rules if r.id != rule_id]
            return len(self.rules) != before

    def clear(self) -> None:
        with self._lock:
            self.rules.clear()
