"""Telegram alerts and commands (via a @BotFather bot). Only the configured chat may issue commands."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("bot.notify")


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.enabled = bool(token and chat_id)
        self._offset: int | None = None
        self._http = httpx.Client(timeout=10)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str) -> None:
        if not self.enabled:
            log.info("[telegram disabled] %s", text)
            return
        try:
            self._http.post(self._url("sendMessage"), json={
                "chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True,
            }).raise_for_status()
        except Exception as e:  # noqa: BLE001: alerts must never crash the bot
            log.warning("Telegram send failed: %s", type(e).__name__)

    def poll_commands(self) -> list[str]:
        """Return new command texts from the configured chat. Messages from anyone else are ignored."""
        if not self.enabled:
            return []
        params = {"timeout": 0, "allowed_updates": '["message"]'}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            r = self._http.get(self._url("getUpdates"), params=params)
            r.raise_for_status()
            updates = r.json().get("result", [])
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram poll failed: %s", type(e).__name__)
            return []
        cmds = []
        for u in updates:
            self._offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                cmds.append(text)
        return cmds


if __name__ == "__main__":
    # Helper: after messaging your bot once, print the chat id to put in TELEGRAM_CHAT_ID.
    import os

    from dotenv import load_dotenv

    from bot.config import ROOT

    load_dotenv(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first.")
    r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10).json()
    chats = {(u["message"]["chat"]["id"], u["message"]["chat"].get("username") or u["message"]["chat"].get("title"))
             for u in r.get("result", []) if "message" in u}
    if not chats:
        raise SystemExit("No messages yet. Send any message to your bot in Telegram, then rerun.")
    for cid, name in chats:
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")
