"""Manual approval queue for trades above the approval threshold."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.store import Store, now_iso


class Approvals:
    def __init__(self, store: Store, ttl_minutes: float):
        self.store = store
        self.ttl = timedelta(minutes=ttl_minutes)

    def request(self, signal_id: int) -> int:
        now = datetime.now(timezone.utc)
        cur = self.store.execute(
            "INSERT INTO approvals(signal_id, created, expires, status) VALUES(?,?,?, 'pending')",
            (signal_id, now.isoformat(timespec="seconds"), (now + self.ttl).isoformat(timespec="seconds")),
        )
        self.store.update_signal(signal_id, status="pending_approval")
        return int(cur.lastrowid)

    def pending(self) -> list[dict]:
        return self.store.query(
            "SELECT a.*, s.symbol, s.qty, s.price, s.notional, s.stop, s.take_profit FROM approvals a "
            "JOIN signals s ON s.id = a.signal_id WHERE a.status='pending' ORDER BY a.id"
        )

    def decide(self, approval_id: int, approve: bool, by: str) -> tuple[bool, str]:
        rows = self.store.query("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not rows:
            return False, f"No approval #{approval_id}"
        a = rows[0]
        if a["status"] != "pending":
            return False, f"Approval #{approval_id} is already {a['status']}"
        if datetime.fromisoformat(a["expires"]) <= datetime.now(timezone.utc):
            self._set(approval_id, "expired", "system")
            self.store.update_signal(a["signal_id"], status="expired")
            return False, f"Approval #{approval_id} expired"
        status = "approved" if approve else "rejected"
        self._set(approval_id, status, by)
        if not approve:
            self.store.update_signal(a["signal_id"], status="rejected", reason=f"rejected by {by}")
        self.store.event("approval", f"#{approval_id} {status} by {by}")
        return True, f"Approval #{approval_id} {status}"

    def approved_ready(self) -> list[dict]:
        return self.store.query("SELECT * FROM approvals WHERE status='approved' ORDER BY id")

    def mark_executed(self, approval_id: int) -> None:
        self.store.execute("UPDATE approvals SET status='executed' WHERE id=?", (approval_id,))

    def expire_old(self) -> list[int]:
        now = now_iso()
        rows = self.store.query("SELECT id, signal_id FROM approvals WHERE status='pending' AND expires <= ?", (now,))
        for r in rows:
            self._set(r["id"], "expired", "system")
            self.store.update_signal(r["signal_id"], status="expired", reason="approval timed out")
        return [r["id"] for r in rows]

    def _set(self, approval_id: int, status: str, by: str) -> None:
        self.store.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                           (status, by, now_iso(), approval_id))
