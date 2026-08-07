"""
Discord alert for the Manual/Strategic Trade Learning Layer (contrarian
big-win case studies). Deliberately self-contained and separate from
alerts/discord_formatter.py - this is a distinct layer per the user's own
framing ("this is not on the wallet intelligence layer"), and keeping it
fully separate means the existing wallet/market alert code in
alerts/discord_formatter.py is never touched by this feature.

Uses its own webhook (DISCORD_STRATEGY_LEARNING_WEBHOOK_URL, falling back
to the wallet webhook if unset) and its own send/format functions.
"""

import requests

from config.loader import DISCORD_STRATEGY_LEARNING_WEBHOOK_URL
from config.cost_profile import CostProfile, register
from alerts.discord_formatter import _enforce_embed_char_limit  # shared safety net only - no other coupling, see module docstring

MODULE_COST_PROFILE = register(CostProfile(
    module_name="strategy_learning.contrarian_alert_formatter",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - Discord webhooks are free; no paid path.",
))


def send_contrarian_win_alert(wallet_address: str, username: str, contrarian_win: dict,
                               wallet_context: dict = None, longshot_pattern: dict = None) -> bool:
    if not DISCORD_STRATEGY_LEARNING_WEBHOOK_URL:
        print("Strategy learning: no webhook configured - skipping contrarian win alert.")
        return False
    if not contrarian_win:
        return False

    embed = _build_embed(wallet_address, username, contrarian_win, wallet_context, longshot_pattern)
    return _post({"embeds": [embed]})


def _format_holding_days(w: dict) -> str:
    days = w.get("holding_days")
    if days is None:
        return "unknown"
    if w.get("was_live_entry"):
        return "entered during live/in-play trading (after the market's listed close time)"
    return f"{days} days (to resolution)"


def _build_embed(wallet_address: str, username: str, w: dict, wallet_context: dict = None,
                  longshot_pattern: dict = None) -> dict:
    name = username or f"{wallet_address[:10]}…"
    payout_str = f"{w['payout_multiple']}x" if w.get("payout_multiple") is not None else "n/a"

    similar_lines = "\n".join(
        f"• \"{e['title']}\" → resolved {e.get('resolved_outcome', 'unknown')} (similarity {e['similarity']})"
        for e in w.get("similar_past_events", [])[:3]
    ) or "No sufficiently similar past events found."

    fields = [
        {"name": "📬 Wallet", "value": f"`{wallet_address}` ({name})", "inline": False},
        {"name": "Market (featured win)", "value": w.get("market_title", "unknown"), "inline": False},
        {"name": "Outcome bought", "value": w.get("outcome_bought", "unknown"), "inline": True},
        {"name": "Entry price (crowd's implied odds)", "value": f"${w.get('entry_price', 0):.2f} ({w.get('crowd_implied_probability_pct', 0)}%)", "inline": True},
        {"name": "Payout", "value": f"${w.get('realized_pnl', 0):,.0f} ({payout_str} return)", "inline": True},
        {"name": "Entered", "value": w.get("entry_time_display", "unknown"), "inline": True},
        {"name": "Held for", "value": _format_holding_days(w), "inline": True},
        {"name": "🧠 Speculated strategy (not confirmed)", "value": w.get("speculated_rationale", "")[:1000], "inline": False},
    ]

    # This is the direct fix for "only shows one position when the wallet
    # entered multiple markets": the featured win above is just the single
    # BIGGEST result for headline purposes, but if this wallet has a
    # REPEATED low-entry/small-stake pattern (features.wallet_features.
    # compute_longshot_pattern), list every qualifying position here as
    # the actual evidence of a pattern - not a one-hit wonder - with real
    # entry/exit times and a readable strategy-type label per trade.
    if longshot_pattern and longshot_pattern.get("trades"):
        trades = longshot_pattern["trades"]
        lines = []
        for t in trades[:6]:
            exit_display = t["exit_time_display"] if t.get("exit_timestamp") else "still open"
            pnl = t.get("realized_pnl")
            pnl_str = f"${pnl:,.0f}" if pnl is not None else "n/a"
            lines.append(
                f"• **{t['market_title']}** — bought {t['outcome']} @ ${t['entry_price']:.2f} on "
                f"{t['entry_time_display']}, exited {exit_display} — {pnl_str} — _{t['strategy_type']}_"
            )
        if len(trades) > 6:
            lines.append(f"…and {len(trades) - 6} more qualifying trade(s).")
        fields.append({
            "name": f"📈 Full trading pattern ({longshot_pattern['longshot_trade_count']} low-entry/small-stake "
                    f"trades, {(longshot_pattern.get('longshot_win_rate') or 0) * 100:.0f}% win rate, "
                    f"${longshot_pattern.get('longshot_total_pnl_usd', 0):,.0f} net) — NOT a one-hit wonder",
            "value": "\n".join(lines)[:1000],
            "inline": False,
        })

    fields.append({"name": "📚 Similar past events (for context)", "value": similar_lines[:1000], "inline": False})

    # Additional context (NOT the signal this alert is about): the
    # wallet's OVERALL copy-trade suitability from wallet_intel, shown
    # separately from the case-study framing above so it's clear this one
    # big win isn't itself being claimed as evidence the wallet is
    # copy-worthy - it's a separate, independently-computed verdict.
    if wallet_context:
        rec_label = wallet_context.get("copy_trade_recommendation_label", "unknown")
        score = wallet_context.get("copy_trade_score")
        score_str = f"{score}/100" if score is not None else "n/a"
        fields.append({
            "name": "📊 Wallet's overall copy-trade suitability (separate verdict, for context only)",
            "value": f"{rec_label} ({score_str}) — {wallet_context.get('behavior_label', 'unclassified')}",
            "inline": False,
        })

    return {
        "title": f"🎯 Contrarian Big Win — {name}",
        "color": 0x9B59B6,
        "fields": fields,
        "footer": {"text": "Strategic Trade Learning Layer · for manual strategy analysis, not a copy-trade signal"},
    }


def _post(payload: dict) -> bool:
    if payload.get("embeds"):
        payload["embeds"] = [_enforce_embed_char_limit(e) for e in payload["embeds"]]
    try:
        resp = requests.post(DISCORD_STRATEGY_LEARNING_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code >= 300:
            print(f"WARNING: strategy learning webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"WARNING: strategy learning webhook failed: {e}")
        return False
