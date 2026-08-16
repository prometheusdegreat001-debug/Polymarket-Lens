"""
Builds CTA button lists from config/discord.yml's label/URL templates.
Pure string formatting - no external calls.
"""

from config.loader import discord as discord_cfg
from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="alerts.cta_builder",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - pure string templating from config, no external calls.",
))


def build_ctas(market_url: str, source_urls: list, wallet_addresses: list) -> list:
    """
    Returns a list of {"label": str, "url": str} dicts, respecting
    discord.yml's show_market_links / show_wallet_addresses toggles.
    """
    ctas = []
    labels = discord_cfg.cta_labels
    templates = discord_cfg.cta_url_templates

    if discord_cfg.show_market_links and market_url:
        ctas.append({"label": labels.open_market, "url": templates.open_market.format(market_url=market_url)})

    if source_urls:
        # Link to the first/most relevant source for "Review Sources"
        ctas.append({"label": labels.review_sources, "url": source_urls[0]})

    if discord_cfg.show_wallet_addresses and wallet_addresses:
        first_wallet = wallet_addresses[0]
        ctas.append({
            "label": labels.view_wallet,
            "url": templates.view_wallet.format(wallet_address=first_wallet),
        })

    return ctas
