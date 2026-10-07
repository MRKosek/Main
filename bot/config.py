"""Load strategy.md's config block and environment settings, and enforce paper-first."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
STRATEGY_PATH = ROOT / "strategy.md"
LIVE_ACK_PHRASE = "I_ACCEPT_LIVE_RISK"

_BLOCK_RE = re.compile(r"```json strategy-config\s*\n(.*?)\n```", re.DOTALL)


class ConfigError(RuntimeError):
    pass


def load_strategy_config(path: Path = STRATEGY_PATH) -> dict:
    text = path.read_text()
    match = _BLOCK_RE.search(text)
    if not match:
        raise ConfigError(f"No ```json strategy-config block found in {path}")
    cfg = json.loads(match.group(1))
    for section in ("risk", "strategy", "backtest", "jev"):
        if section not in cfg:
            raise ConfigError(f"strategy-config is missing the '{section}' section")
    _validate_risk(cfg["risk"])
    return cfg


def write_strategy_section(new_strategy: dict, validated: bool, path: Path = STRATEGY_PATH) -> None:
    """Replace the `strategy` section of the config block (used by the backtester)."""
    text = path.read_text()
    match = _BLOCK_RE.search(text)
    if not match:
        raise ConfigError(f"No strategy-config block in {path}")
    cfg = json.loads(match.group(1))
    cfg["strategy"] = new_strategy
    cfg["validated"] = validated
    block = "```json strategy-config\n" + json.dumps(cfg, indent=2) + "\n```"
    path.write_text(text[: match.start()] + block + text[match.end():])


def _validate_risk(risk: dict) -> None:
    required = {
        "max_position_usd", "max_open_positions", "daily_loss_limit_usd",
        "approval_threshold_usd", "approval_ttl_minutes", "max_consecutive_errors",
    }
    missing = required - risk.keys()
    if missing:
        raise ConfigError(f"Risk rules missing from strategy.md: {sorted(missing)}. Nothing runs without them.")
    for key in required:
        if not isinstance(risk[key], (int, float)) or risk[key] <= 0:
            raise ConfigError(f"Risk rule {key} must be a positive number, got {risk[key]!r}")


@dataclass
class Settings:
    alpaca_key: str
    alpaca_secret: str
    paper: bool
    typesafe_key: str
    telegram_token: str
    telegram_chat_id: str
    dashboard_token: str
    dashboard_host: str
    dashboard_port: int
    db_path: Path
    jev_price_in_per_m: float
    jev_price_out_per_m: float
    strategy: dict = field(default_factory=dict)


def load_settings(require_trading_keys: bool = True) -> Settings:
    load_dotenv(ROOT / ".env")
    env = os.environ.get

    paper = env("ALPACA_PAPER", "true").strip().lower() != "false"
    if not paper and env("LIVE_TRADING_ACK", "").strip() != LIVE_ACK_PHRASE:
        raise ConfigError(
            "ALPACA_PAPER=false but LIVE_TRADING_ACK is not set. Live trading is locked. "
            "Run the final check in README.md first."
        )

    settings = Settings(
        alpaca_key=env("ALPACA_API_KEY", "").strip(),
        alpaca_secret=env("ALPACA_SECRET_KEY", "").strip(),
        paper=paper,
        typesafe_key=env("TYPESAFE_API_KEY", "").strip(),
        telegram_token=env("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=env("TELEGRAM_CHAT_ID", "").strip(),
        dashboard_token=env("DASHBOARD_TOKEN", "").strip(),
        dashboard_host=env("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=int(env("DASHBOARD_PORT", "8000")),
        db_path=ROOT / env("BOT_DB_PATH", "data/bot.sqlite3"),
        jev_price_in_per_m=float(env("JEV_PRICE_INPUT_PER_M", "0.042")),
        jev_price_out_per_m=float(env("JEV_PRICE_OUTPUT_PER_M", "0")),
        strategy=load_strategy_config(),
    )
    if require_trading_keys:
        missing = [n for n, v in [
            ("ALPACA_API_KEY", settings.alpaca_key),
            ("ALPACA_SECRET_KEY", settings.alpaca_secret),
            ("TYPESAFE_API_KEY", settings.typesafe_key),
        ] if not v]
        if missing:
            raise ConfigError(f"Missing in .env: {', '.join(missing)}")
    return settings
