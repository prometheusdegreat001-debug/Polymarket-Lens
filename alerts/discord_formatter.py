"""
Formats a DiscordAlertPayload as a Discord embed and posts it via webhook.
Same webhook mechanism as v1 - no bot token needed.
"""

import requests

from config.loader import DISCORD_WEBHOOK_URL, DISCORD_WALLET_WEBHOOK_URL
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="alerts.discord_formatter",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - Discord webhooks are free; this module has no paid path.",
))

_DECISION_COLOR = {
    "BUY_YES": 0x2ECC71, "BUY_NO": 0xE74C3C,
    "MONITOR": 0xF1C40F, "NO_TRADE": 0x95A5A6,
}


def send_market_alert(payload: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("Discord: DISCORD_WEBHOOK_URL not set - skipping market alert.")
        return False

    embed = _build_market_embed(payload)
    return _post(DISCORD_WEBHOOK_URL, {"embeds": [embed]})


def send_wallet_alert(wallet_profile: dict) -> bool:
    if not DISCORD_WALLET_WEBHOOK_URL:
        print("Discord: no wallet webhook configured - skipping wallet alert.")
        return False

    embed = _build_wallet_embed(wallet_profile)
    return _post(DISCORD_WALLET_WEBHOOK_URL, {"embeds": [embed]})


def _build_market_embed(payload: dict) -> dict:
    color = _DECISION_COLOR.get(payload["decision_label"], 0x95A5A6)

    fields = [
        {"name": "Market", "value": payload["title"][:250], "inline": False},
        {"name": "📋 Decision", "value": payload.get("decision_statement", "")[:1000], "inline": False},
        {"name": "In plain terms", "value": payload["plain_explanation"][:1000], "inline": False},
        {"name": "Decision", "value": f"{payload['decision_label']} (confidence: {payload['confidence']})", "inline": True},
        {"name": "Suggested size", "value": f"{payload['suggested_size_pct']:.1f}% of risk budget", "inline": True},
        {"name": "Evidence", "value": payload["evidence_summary"][:800], "inline": False},
        {"name": "Historical precedent", "value": payload["historical_summary"][:800], "inline": False},
        {"name": "Wallet intelligence", "value": payload["wallet_summary"][:800], "inline": False},
        {"name": "Main risks", "value": payload["main_risks"][:500], "inline": False},
        {"name": "What would invalidate this", "value": payload["failure_conditions"][:500], "inline": False},
    ]

    if payload.get("wallet_addresses"):
        fields.append({
            "name": "Wallet address(es)",
            "value": "\n".join(f"`{w}`" for w in payload["wallet_addresses"][:5]),
            "inline": False,
        })

    embed = {
        "title": payload["title"][:250],
        "color": color,
        "fields": fields,
        "footer": {"text": "Polymarket Alpha Intelligence Engine · research signal, not financial advice"},
    }

    if payload.get("cta_buttons"):
        # Discord embeds don't support real buttons via webhook - represent
        # CTAs as a clearly labeled links field instead.
        links_text = "\n".join(f"[{c['label']}]({c['url']})" for c in payload["cta_buttons"])
        embed["fields"].append({"name": "Actions", "value": links_text, "inline": False})

    return embed


def _build_wallet_embed(wallet_profile: dict) -> dict:
    address = wallet_profile.get("wallet_address", "")
    name = wallet_profile.get("username") or (address[:10] + "…" if address else "unknown")
    verdict_emoji = {"copy": "✅", "watch": "🟡", "avoid": "⚠️"}.get(
        wallet_profile.get("copy_trade_recommendation"), "🟡"
    )
    rec_label = wallet_profile.get("copy_trade_recommendation_label", "Conditional")
    score_10 = wallet_profile.get("copy_trade_score_10", round(wallet_profile.get("copy_trade_score", 0) / 10, 1))

    fields = [
        {"name": "📬 Wallet Address", "value": f"`{address}`" if address else "N/A", "inline": False},
        {"name": "Behavior label", "value": wallet_profile.get("behavior_label", "unknown"), "inline": True},
        {"name": "Score", "value": f"{score_10}/10", "inline": True},
        {"name": "Copy Trading?", "value": rec_label, "inline": True},
        {"name": "Win rate (lifetime)", "value": _fmt_pct(wallet_profile.get("win_rate")), "inline": True},
        {"name": "PnL (lifetime)", "value": f"${wallet_profile.get('pnl', wallet_profile.get('pnl_lifetime', 0)):,.0f}", "inline": True},
        {"name": "🕐 Last active", "value": _fmt_recency(wallet_profile.get("days_since_last_trade")), "inline": True},
        {
            "name": "📅 Last 14 days",
            "value": wallet_profile.get("recent_14d_summary", "No recent activity data.")[:500],
            "inline": False,
        },
        {
            "name": "Biggest win / loss",
            "value": f"+${wallet_profile.get('biggest_win_usd', 0):,.0f} / "
                     f"${wallet_profile.get('biggest_loss_usd', 0):,.0f}",
            "inline": True,
        },
        {"name": "Sample size", "value": wallet_profile.get("sample_quality", "N/A")[:200], "inline": False},
    ]

    category_perf = wallet_profile.get("category_performance")
    if category_perf:
        if isinstance(category_perf, str):
            import json
            try:
                category_perf = json.loads(category_perf)
            except (ValueError, TypeError):
                category_perf = {}
        if category_perf:
            cat_lines = []
            for cat, stats in sorted(category_perf.items(), key=lambda kv: -(kv[1].get("pnl") or 0)):
                wr = f"{stats['win_rate']*100:.0f}%" if stats.get("win_rate") is not None else "N/A"
                cat_lines.append(f"• {cat}: {wr} win rate, ${stats.get('pnl', 0):,.0f} PnL ({stats.get('resolved_count', 0)} trades)")
            fields.append({
                "name": "📊 Category specialization",
                "value": "\n".join(cat_lines)[:1000],
                "inline": False,
            })

    fields.append({
        "name": f"{verdict_emoji} {wallet_profile.get('copy_trade_recommendation', 'watch').upper()}",
        "value": wallet_profile.get("why_copy_or_not", "")[:1000],
        "inline": False,
    })

    return {
        "title": f"🟢 New wallet candidate — {name}",
        "description": wallet_profile.get("full_report_text", "")[:4000],
        "color": 0x2ECC71,
        "fields": fields,
        "footer": {"text": "Polymarket Wallet Intelligence · research signal, not financial advice"},
    }


def _fmt_pct(val):
    return f"{val*100:.0f}%" if val is not None else "N/A"


def _fmt_recency(days):
    if days is None or days == float("inf"):
        return "unknown"
    if days < 1:
        return "today"
    if days <= 7:
        return f"{days:.0f} days ago"
    if days <= 14:
        return f"{days:.0f} days ago ⚠️"
    return f"{days:.0f} days ago ⚠️ DORMANT"


def _post(webhook_url: str, payload: dict) -> bool:
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code >= 300:
            print(f"WARNING: Discord webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"WARNING: Discord webhook failed: {e}")
        return False
