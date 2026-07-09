from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from borex.config import LiveConfig


@dataclass
class KillSwitchState:
    killed: bool = False
    reason: str = ""
    consecutive_errors: int = 0
    killed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "killed": self.killed,
            "reason": self.reason,
            "consecutive_errors": self.consecutive_errors,
            "killed_at": self.killed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> KillSwitchState:
        if not raw:
            return cls()
        return cls(
            killed=bool(raw.get("killed", False)),
            reason=str(raw.get("reason", "")),
            consecutive_errors=int(raw.get("consecutive_errors", 0)),
            killed_at=raw.get("killed_at"),
        )


@dataclass
class KillSwitch:
    state: KillSwitchState = field(default_factory=KillSwitchState)

    @property
    def killed(self) -> bool:
        return self.state.killed

    def trip(self, reason: str) -> None:
        if self.state.killed:
            return
        self.state.killed = True
        self.state.reason = reason
        self.state.killed_at = datetime.now(timezone.utc).isoformat()

    def reset(self) -> None:
        self.state = KillSwitchState()

    def record_success(self) -> None:
        self.state.consecutive_errors = 0

    def record_error(self, config: LiveConfig) -> bool:
        """Increment error count; trip kill-switch if threshold exceeded."""
        self.state.consecutive_errors += 1
        if self.state.consecutive_errors >= config.max_consecutive_errors:
            self.trip(f"consecutive_errors_{self.state.consecutive_errors}")
            return True
        return False


def check_stale_data(last_bar_ts: str | None, stale_minutes: int) -> tuple[bool, float | None]:
    """Return (is_stale, age_minutes)."""
    if not last_bar_ts or stale_minutes <= 0:
        return False, None
    try:
        ts = datetime.fromisoformat(str(last_bar_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        return age > stale_minutes, round(age, 1)
    except (TypeError, ValueError):
        return True, None
