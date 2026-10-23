"""Watch every grill shutdown for the firmware's fan cool-down.

When a GMG is powered off it normally stops the auger and runs its fan for a few
minutes to burn out and clear the firepot, then turns itself off. A shutdown on
24 Oct 2026 skipped that purge (power went straight from on to off at a ~120C
pit) and left the fire to smoulder out. The grill has no "fan only" command, so
the planner cannot run the purge itself; it watches for it and alerts if it is
missing.

The watch is driven by the pit climate entity, so it covers a power-off from
Home Assistant, the GMG app or the grill's own button alike. It is pure (no Home
Assistant imports) so it can be tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .planner import RecipeEvent

IDLE = "idle"
STOPPING = "stopping"  # power-off seen, waiting for the fan cool-down to start
COOLING = "cooling"  # fan cool-down running
COMPLETE = "complete"  # cool-down finished (or the grill was already cool)
INCOMPLETE = "incomplete"  # stopped without a cool-down while still hot
STATES = [IDLE, STOPPING, COOLING, COMPLETE, INCOMPLETE]

# The cool-down showed up 30-40 s after the power-off frame on 16 Oct; allow a
# few polls of slack before calling it missing.
FAN_WAIT_S = 90
# Below this the firepot is out and the grill is safe to leave.
SAFE_PIT_C = 65.0
HISTORY_KEEP = 20


@dataclass
class ShutdownWatch:
    """State machine fed with the pit climate's state each refresh."""

    state: str = IDLE
    started: datetime | None = None
    pit_at_start: float | None = None
    fan_seen_at: datetime | None = None
    finished: datetime | None = None
    was_heating: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)

    def update(
        self, now: datetime, *, mode: str | None, action: str | None, pit_c: float | None
    ) -> list[RecipeEvent]:
        """Advance with the climate's ``mode`` (state) and ``hvac_action``.

        ``mode`` is None while the grill is unavailable: nothing is decided then.
        """
        events: list[RecipeEvent] = []
        if mode is None:
            return events
        if mode == "heat":
            if self.state != IDLE:
                self.state = IDLE  # lit again: a new cook, a new shutdown to watch
            self.was_heating = True
            return events
        if self.was_heating and self.state in (IDLE, COMPLETE):
            self.state = STOPPING
            self.started = now
            self.pit_at_start = round(pit_c, 1) if pit_c is not None else None
            self.fan_seen_at = self.finished = None
        self.was_heating = False

        fan = action == "fan"
        waited = (now - self.started).total_seconds() if self.started else FAN_WAIT_S
        if self.state == STOPPING:
            if fan:
                self._cooling(now, events)
            elif waited >= FAN_WAIT_S:
                if pit_c is not None and pit_c > SAFE_PIT_C:
                    self.state = INCOMPLETE
                    self._record(fan=False)
                    events.append(
                        RecipeEvent(
                            f"shutdown_incomplete@{self.started.isoformat() if self.started else ''}",
                            "prompt",
                            "Shutdown incomplete",
                            f"The grill stopped without its fan cool-down and the pit is still {pit_c:.0f}C. "
                            "The fire is left to smoulder out. Check the firepot, or start a cool-down from "
                            "the grill's own power button.",
                        )
                    )
                else:
                    self._complete(now, events, fan=False)
        elif self.state == COOLING:
            if not fan:
                self._complete(now, events, fan=True)
        elif self.state == INCOMPLETE:
            if fan:
                self._cooling(now, events)  # a late (or manual) cool-down
            elif pit_c is not None and pit_c < SAFE_PIT_C:
                self.state = IDLE  # cooled on its own
                self.finished = now
        return events

    def _cooling(self, now: datetime, events: list[RecipeEvent]) -> None:
        self.state = COOLING
        self.fan_seen_at = now
        events.append(
            RecipeEvent(
                f"shutdown_cooling@{self.started.isoformat() if self.started else now.isoformat()}",
                "advice",
                "Cool-down running",
                "The auger has stopped and the fan is burning out and clearing the firepot. "
                "The grill turns itself off when it's done.",
            )
        )

    def _complete(self, now: datetime, events: list[RecipeEvent], *, fan: bool) -> None:
        self.state = COMPLETE
        self.finished = now
        fan_min = (now - self.fan_seen_at).total_seconds() / 60 if fan and self.fan_seen_at else None
        self._record(fan=fan, fan_min=fan_min)
        if fan:
            events.append(
                RecipeEvent(
                    f"shutdown_complete@{self.started.isoformat() if self.started else now.isoformat()}",
                    "advice",
                    "Shutdown complete",
                    f"Fan cool-down finished after {fan_min:.0f} min. The grill is off and safe."
                    if fan_min is not None
                    else "Fan cool-down finished. The grill is off and safe.",
                )
            )

    def _record(self, *, fan: bool, fan_min: float | None = None) -> None:
        self.history.append(
            {
                "at": self.started.isoformat() if self.started else None,
                "pit_c": self.pit_at_start,
                "fan_cool_down": fan,
                "fan_min": round(fan_min, 1) if fan_min is not None else None,
            }
        )
        self.history = self.history[-HISTORY_KEEP:]

    def as_dict(self) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "state": self.state,
            "started": iso(self.started),
            "pit_at_start": self.pit_at_start,
            "fan_seen_at": iso(self.fan_seen_at),
            "finished": iso(self.finished),
            "was_heating": self.was_heating,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ShutdownWatch:
        def parse(v: Any) -> datetime | None:
            return datetime.fromisoformat(v) if isinstance(v, str) and v else None

        state = d.get("state", IDLE)
        return cls(
            state=state if state in STATES else IDLE,
            started=parse(d.get("started")),
            pit_at_start=d.get("pit_at_start"),
            fan_seen_at=parse(d.get("fan_seen_at")),
            finished=parse(d.get("finished")),
            was_heating=bool(d.get("was_heating")),
            history=list(d.get("history") or []),
        )
