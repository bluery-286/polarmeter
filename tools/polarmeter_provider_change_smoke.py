#!/usr/bin/env python3
from __future__ import annotations

from polarmeter_cache_snapshot import validate_candidate
from polarmeter_free_provider_probe import yahoo_chart_change_fields


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
    print("PolarMeter provider change smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

