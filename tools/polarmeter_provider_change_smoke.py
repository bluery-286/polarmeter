#!/usr/bin/env python3
from __future__ import annotations

from polarmeter_cache_snapshot import validate_candidate
from polarmeter_free_provider_probe import yahoo_chart_change_fields
from polarmeter_news_rss_probe import cause_aware_display_headline, english_market_context_translation, market_burden_tone


def main() -> int:
    price, change, change_pct, previous, source = yahoo_chart_change_fields(
        {"regularMarketPrice": 7187.91, "regularMarketPreviousClose": 7475.94},
        [8051.33, 7291.91, 7475.94, 7187.91],
        prefer_daily_series_previous=True,
    )
    assert round(price or 0, 2) == 7187.91
    assert round(previous or 0, 2) == 7475.94
    assert round(change or 0, 2) == -288.03
    assert round(change_pct or 0, 2) == -3.85
    assert source == "meta_previous_close"

    price, change, change_pct, previous, source = yahoo_chart_change_fields(
        {
            "regularMarketPrice": 7187.91,
            "previousClose": 8051.33,
            "chartPreviousClose": 8051.33,
        },
        [8051.33, 7291.91, 7475.94, 7187.91],
        prefer_daily_series_previous=True,
    )
    assert round(price or 0, 2) == 7187.91
    assert round(previous or 0, 2) == 7475.94
    assert round(change or 0, 2) == -288.03
    assert round(change_pct or 0, 2) == -3.85
    assert source == "daily_series_previous_close"

    status, reason = validate_candidate(
        "kospi",
        {"status": "ok", "price": 7200, "changePct": -6.01},
    )
    assert status == "suspect"
    assert reason == "changePct_suspect>6.0"

    cpi_relief_headline = (
        "Today’s Market Recap: CPI Cooling Ignites AI Tech Stock Rally, "
        "Nvidia, Micron, AMD Rise Together"
    )
    translated = english_market_context_translation(cpi_relief_headline)
    assert translated == "CPI 둔화에 AI·반도체주 동반 상승"
    assert market_burden_tone(cpi_relief_headline, "neutral") == "positive"
    assert market_burden_tone(translated, "neutral") == "positive"

    wti_relief_headline = "국제유가, 중동 긴장에도 숨고르기…WTI 0.8%↓"
    wti_display = cause_aware_display_headline(wti_relief_headline, wti_relief_headline)
    assert wti_display == f"유가 부담 완화 · {wti_relief_headline}"
    assert market_burden_tone(wti_display, "negative") == "positive"
    print("PolarMeter provider change smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
