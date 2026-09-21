from src.services.model_pricing import estimate_cost_usd


def test_estimate_cost_usd_exact_model_id():
    # claude-haiku-4-5 is $1.00/$5.00 per 1M tokens.
    cost = estimate_cost_usd("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=1_000_000)

    assert cost == 6.00


def test_estimate_cost_usd_matches_dated_snapshot_suffix():
    # Confirmed directly against a real API response earlier this week:
    # requesting "claude-haiku-4-5" gets back response.model ==
    # "claude-haiku-4-5-20251001" — the pricing table only has the plain
    # id, so this must resolve via prefix match, not exact equality.
    cost = estimate_cost_usd("claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=0)

    assert cost == 1.00


def test_estimate_cost_usd_returns_none_for_unknown_model():
    cost = estimate_cost_usd("some-future-model-nobody-has-priced-yet", 1000, 1000)

    assert cost is None
