"""Risk limits and the kill switch. Every entry passes through RiskManager.check_entry."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bot.store import Store, now_iso

KILL_FILE = Path(__file__).resolve().parent.parent / "data" / "KILL"


@dataclass
class RiskVerdict:
    ok: bool
    reason: str
    needs_approval: bool = False


class KillSwitch:
    """Persisted halt flag. Once tripped it stays tripped (across restarts) until manually reset."""

    def __init__(self, store: Store, kill_file: Path = KILL_FILE):
        self.store = store
        self.kill_file = kill_file

    @property
    def tripped(self) -> bool:
        if self.kill_file.exists() and not self.store.get("kill_switch", {}).get("tripped"):
            self.trip("KILL file present")
        return bool(self.store.get("kill_switch", {}).get("tripped"))

    @property
    def reason(self) -> str:
        return self.store.get("kill_switch", {}).get("reason", "")

    def trip(self, reason: str) -> bool:
        """Returns True if this call tripped it (False if it was already tripped)."""
        state = self.store.get("kill_switch", {})
        if state.get("tripped"):
            return False
        self.store.set("kill_switch", {"tripped": True, "reason": reason, "at": now_iso()})
        self.store.event("kill_switch", f"KILL SWITCH: {reason}", level="critical")
        return True

    def reset(self, by: str) -> None:
        if self.kill_file.exists():
            raise RuntimeError("Remove data/KILL before resuming.")
        self.store.set("kill_switch", {"tripped": False, "reason": "", "at": now_iso(), "reset_by": by})
        self.store.set("consecutive_errors", 0)
        self.store.event("kill_switch", f"Kill switch reset by {by}", level="warning")


class RiskManager:
    def __init__(self, risk_cfg: dict, store: Store, kill: KillSwitch):
        self.cfg = risk_cfg
        self.store = store
        self.kill = kill

    def check_entry(self, notional: float, open_positions: int, has_position: bool) -> RiskVerdict:
        if self.kill.tripped:
            return RiskVerdict(False, f"kill switch active: {self.kill.reason}")
        if has_position:
            return RiskVerdict(False, "already holding this symbol")
        if open_positions >= self.cfg["max_open_positions"]:
            return RiskVerdict(False, f"max open positions ({self.cfg['max_open_positions']}) reached")
        if notional <= 0:
            return RiskVerdict(False, "zero size")
        if notional > self.cfg["max_position_usd"]:
            return RiskVerdict(False, f"${notional:,.2f} exceeds max position ${self.cfg['max_position_usd']:,}")
        needs = notional > self.cfg["approval_threshold_usd"]
        return RiskVerdict(True, "ok", needs_approval=needs)

    def check_daily_loss(self, equity: float, last_equity: float) -> bool:
        """Trips the kill switch if today's loss exceeds the limit. Returns True if trading may continue."""
        loss = last_equity - equity
        if loss >= self.cfg["daily_loss_limit_usd"]:
            self.kill.trip(f"daily loss ${loss:,.2f} ≥ limit ${self.cfg['daily_loss_limit_usd']:,}")
            return False
        return True

    def record_error(self, err: str) -> int:
        n = int(self.store.get("consecutive_errors", 0)) + 1
        self.store.set("consecutive_errors", n)
        self.store.event("error", err, level="error")
        if n >= self.cfg["max_consecutive_errors"]:
            self.kill.trip(f"{n} consecutive errors (last: {err[:200]})")
        return n

    def record_ok(self) -> None:
        if self.store.get("consecutive_errors", 0):
            self.store.set("consecutive_errors", 0)
