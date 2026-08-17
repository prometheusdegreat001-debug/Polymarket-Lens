"""
News Direction (spec section 11) - free tier.

IMPORTANT HONESTY NOTE, read before extending this module: reliably
classifying whether a news article is "bullish for YES" vs "bearish for
YES" for a SPECIFIC Polymarket market requires actually understanding
that market's resolution criteria and how the article's content maps
onto it. A naive keyword-based positive/negative classifier CANNOT do
this reliably and can be actively misleading - e.g. "his opponent scored
a decisive win" contains positive-sounding words ("win", "decisive") but
is bearish for the FIRST candidate. Forcing a YES/NO direction out of
keyword counting alone would violate the spec's own explicit rule:
"Never force a directional conclusion when evidence is insufficient."

So this free-tier module deliberately does LESS than the full spec asks
for: it tags an article's general SENTIMENT (positive/negative/neutral) -
useful, honestly free, and clearly not the same claim as "this supports
YES." Mapping sentiment to a specific market's YES/NO framing needs real
language understanding (the market's resolution rule + the article's
actual content), which is a paid-LLM-tier feature not implemented here -
consistent with this codebase's existing verification.yml pattern where
the free tier is honest about being provisional and a paid tier (off by
default) does the real semantic work.

For every article, the "direction" field is "unclear" from this module -
never a fabricated YES/NO leaning. A future LLM-tier module can set this
to a real direction once it exists; nothing downstream should assume
this field means more than it currently does.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="news_intelligence.news_direction",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - see module docstring: this IS the free-tier implementation, deliberately scoped down from the full spec to avoid fabricating unreliable YES/NO calls.",
))

_POSITIVE_WORDS = {
    "win", "wins", "won", "victory", "success", "successful", "approve", "approved", "approval",
    "surge", "surges", "rally", "rallies", "gain", "gains", "boost", "record", "breakthrough",
    "agreement", "deal", "resolve", "resolved", "confirm", "confirmed", "pass", "passed",
}
_NEGATIVE_WORDS = {
    "lose", "loses", "lost", "loss", "fail", "fails", "failed", "failure", "reject", "rejected",
    "rejection", "crash", "crashes", "plunge", "plunges", "decline", "collapse", "scandal",
    "investigation", "charged", "indicted", "resign", "resigns", "resigned", "postpone", "postponed",
    "cancel", "cancelled", "delay", "delayed", "deny", "denied", "denial", "block", "blocked",
}


def classify_sentiment(headline: str, description: str = None) -> dict:
    """
    Returns {"sentiment": "positive"/"negative"/"neutral", "direction": "unclear",
    "confidence": "low", "note": "..."}. "direction" is ALWAYS "unclear" from
    this free-tier module - see module docstring for why.
    """
    text = f"{headline or ''} {description or ''}".lower()
    words = set(text.replace(",", " ").replace(".", " ").split())

    pos_hits = len(words & _POSITIVE_WORDS)
    neg_hits = len(words & _NEGATIVE_WORDS)

    if pos_hits == 0 and neg_hits == 0:
        sentiment = "neutral"
    elif pos_hits > neg_hits:
        sentiment = "positive"
    elif neg_hits > pos_hits:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    return {
        "sentiment": sentiment,
        "direction": "unclear",  # deliberately never fabricated - see docstring
        "confidence": "low",     # free-tier keyword sentiment is inherently low-confidence
        "note": (
            "General sentiment only, from free keyword matching - NOT a reliable YES/NO "
            "direction call for this specific market. Read the article before treating this "
            "as evidence either way."
        ),
    }
