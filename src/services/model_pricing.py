# WHY THIS IS ITS OWN MODULE, NOT A DICT INLINE IN review_agent.py:
# unlike every other constant in this project, this one has an ongoing
# maintenance cost that has nothing to do with THIS project's own code —
# Anthropic revises per-model pricing on its own schedule (this table was
# already found to be stale once, mid-project: Sonnet 5's introductory
# pricing ended August 31, 2026, moving it from $2.00/$10.00 to
# $3.00/$15.00 per 1M tokens). Isolating it in one small, obviously-named
# file is what makes "go update the pricing" a one-file, five-minute task
# instead of a hunt through review_agent.py's actual logic.
#
# Prices are USD per 1,000,000 tokens, (input, output), current as of
# 2026-09-21 (see docs/learning-plan-final-September.md's technology
# update notes for where this was last cross-checked).
_PRICE_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Sorted longest-name-first so a prefix match below picks the more
# specific id when one model id is itself a prefix of another (there's no
# real collision in the table above today, but this is cheap insurance
# against one appearing as new model ids get added).
_KNOWN_MODEL_IDS_BY_LENGTH = sorted(_PRICE_PER_MILLION_TOKENS, key=len, reverse=True)


# Summary: estimates a review's USD cost from token counts and the model
# actually used. Exists so a review's response can report a real cost
# figure without every caller having to know Anthropic's current pricing
# themselves.
#
# WHY THIS MATCHES BY PREFIX, NOT EXACT EQUALITY:
# the model id THIS project configures (settings.anthropic_model, e.g.
# "claude-haiku-4-5") is not always the same string Anthropic's response
# echoes back — confirmed directly while testing the Anthropic API key
# earlier this week, where a request for "claude-haiku-4-5" came back
# with response.model == "claude-haiku-4-5-20251001" (a dated snapshot
# suffix). Exact-matching against the pricing table would silently price
# every real response as "unknown model" and always return None.
def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    for known_id in _KNOWN_MODEL_IDS_BY_LENGTH:
        if model.startswith(known_id):
            input_price, output_price = _PRICE_PER_MILLION_TOKENS[known_id]
            return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
    # An unrecognized model — a caller-supplied model this project has no
    # pricing data for, or a new model id this table hasn't been updated
    # for yet — returns None, not a guessed or zero cost. See
    # ReviewUsage.estimated_cost_usd's own comment for why that distinction
    # matters.
    return None
