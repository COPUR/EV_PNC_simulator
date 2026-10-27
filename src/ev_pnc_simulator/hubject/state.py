"""What the Hubject mock remembers: EVSEs, sessions and charge detail records."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class Evse:
    id: str
    operator_id: str
    compatible: bool = True  # Hubject's IsHubjectCompatible
    out_of_service: bool = False
    session_id: str | None = None
    reserved_by: str | None = None

    @property
    def status(self) -> str:
        if self.out_of_service:
            return "OutOfService"
        if self.session_id:
            return "Occupied"
        if self.reserved_by:
            return "Reserved"
        return "Available"


@dataclass
class Session:
    id: str
    evse_id: str
    provider_id: str
    identification: dict[str, Any]
    started: datetime
    stopped: datetime | None = None
    energy_kwh: float = 10.0


# Synthetic corridor EVSEs. The IDs are made up; they only have to be valid EvseIDs.
DEFAULT_EVSES = [
    Evse("TR*ZES*E10001", "TR*ZES"),
    Evse("TR*ZES*E10002", "TR*ZES", compatible=False),
    Evse("TR*ZES*E10003", "TR*ZES", out_of_service=True),
    Evse("TR*TRG*E20001", "TR*TRG"),
]


@dataclass
class World:
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    evses: dict[str, Evse] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    reservations: dict[str, str] = field(default_factory=dict)  # reservation session id -> EVSE id

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.evses = {e.id: Evse(e.id, e.operator_id, e.compatible, e.out_of_service) for e in DEFAULT_EVSES}
        self.sessions.clear()
        self.reservations.clear()

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def start(self, evse: Evse, provider_id: str, identification: dict[str, Any]) -> Session:
        s = Session(self.new_session_id(), evse.id, provider_id, identification, self.clock())
        self.sessions[s.id] = s
        evse.session_id = s.id
        return s

    def stop(self, session: Session) -> None:
        session.stopped = self.clock()
        evse = self.evses.get(session.evse_id)
        if evse and evse.session_id == session.id:
            evse.session_id = None

    def cdr_fields(self, s: Session) -> dict[str, Any]:
        assert s.stopped is not None
        return {
            "SessionID": s.id,
            "EvseID": s.evse_id,
            "Identification": s.identification,
            "SessionStart": iso(s.started),
            "ChargingStart": iso(min(s.started + timedelta(seconds=10), s.stopped)),
            "ChargingEnd": iso(s.stopped),
            "SessionEnd": iso(s.stopped),
            "ConsumedEnergy": s.energy_kwh,
        }
