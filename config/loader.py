"""
Central config loader. Reads all config/*.yml files once at import time and
exposes them as simple attribute-accessible namespaces, with environment
variables able to override any leaf value.

Override pattern: <SECTION>_<KEY_UPPER>, e.g.
  RISK_MIN_LIQUIDITY_USD=2000        overrides risk.yml -> min_liquidity_usd
  WALLET_SCORING_MIN_PNL_USD=5000    overrides wallet_scoring.yml -> min_pnl_usd
  DISCORD_ALERT_MIN_DEVIATION=0.05   overrides discord.yml -> alert_min_deviation

Nested keys (e.g. weights.win_rate) are not individually env-overridable -
edit the YAML directly for those, since they're rarely tuned in production
the way top-level thresholds are.
"""

import os
import yaml
from pathlib import Path
from types import SimpleNamespace

_CONFIG_DIR = Path(__file__).parent


def _load_yaml(filename: str) -> dict:
    path = _CONFIG_DIR / filename
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _apply_env_overrides(section_prefix: str, data: dict) -> dict:
    """Only overrides top-level (non-nested) keys via env vars."""
    result = dict(data)
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            continue  # nested structures are YAML-only, not env-overridable
        env_key = f"{section_prefix}_{key.upper()}"
        if env_key in os.environ:
            raw = os.environ[env_key]
            # Best-effort type coercion matching the YAML value's type
            if isinstance(value, bool):
                result[key] = raw.lower() in ("1", "true", "yes")
            elif isinstance(value, int):
                result[key] = int(raw)
            elif isinstance(value, float):
                result[key] = float(raw)
            else:
                result[key] = raw
    return result


def _to_namespace(data: dict) -> SimpleNamespace:
    """Recursively converts dicts to attribute-accessible namespaces."""
    converted = {}
    for k, v in data.items():
        if isinstance(v, dict):
            converted[k] = _to_namespace(v)
        else:
            converted[k] = v
    return SimpleNamespace(**converted)


_risk_raw = _apply_env_overrides("RISK", _load_yaml("risk.yml"))
_wallet_scoring_raw = _apply_env_overrides("WALLET_SCORING", _load_yaml("wallet_scoring.yml"))
_verification_raw = _apply_env_overrides("VERIFICATION", _load_yaml("verification.yml"))
_market_categories_raw = _apply_env_overrides("MARKET_CATEGORIES", _load_yaml("market_categories.yml"))
_discord_raw = _apply_env_overrides("DISCORD", _load_yaml("discord.yml"))
_cost_raw = _apply_env_overrides("COST", _load_yaml("cost.yml"))

# cost.yml's enable_paid_research is the SINGLE master switch for every
# paid-tier module in the system (verification, historical_context's LLM
# fallback, etc.) - injected into `verification.enabled` here so those
# modules only need to check one flag, but you only ever need to flip one
# switch in one file to turn the whole paid tier on or off.
_verification_raw["enabled"] = bool(_cost_raw.get("enable_paid_research", False))

risk = _to_namespace(_risk_raw)
wallet_scoring = _to_namespace(_wallet_scoring_raw)
verification = _to_namespace(_verification_raw)
market_categories = _to_namespace(_market_categories_raw)
discord = _to_namespace(_discord_raw)
cost = _to_namespace(_cost_raw)

# Secrets stay in env vars only, never in YAML.
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_WALLET_WEBHOOK_URL = os.environ.get("DISCORD_WALLET_WEBHOOK_URL", "") or DISCORD_WEBHOOK_URL
# Optional separate channel for the manual/strategic trade learning layer
# (contrarian big-win case studies) - falls back to the wallet webhook if
# unset, same fallback pattern as above.
DISCORD_STRATEGY_LEARNING_WEBHOOK_URL = os.environ.get("DISCORD_STRATEGY_LEARNING_WEBHOOK_URL", "") or DISCORD_WALLET_WEBHOOK_URL
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Scheduling (not category-specific, kept top-level)
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", "900"))
MAX_KALSHI_PER_SCAN = int(os.environ.get("MAX_KALSHI_PER_SCAN", "500"))
WALLET_SCAN_EVERY_N_RUNS = int(os.environ.get("WALLET_SCAN_EVERY_N_RUNS", "4"))
DB_PATH = os.environ.get("DB_PATH", "data/mispricing.db")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DATA_API_BASE = "https://data-api.polymarket.com"
POLYMARKET_WEB_BASE = "https://polymarket.com"

REQUEST_TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.5
