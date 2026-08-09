#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polarmeter_cache_snapshot import (
    MAX_STALE_SIGNAL_AGE_HOURS,
    US_ACTIVE_MARKET_STALE_KEYS,
    hard_stale_reason,
    signal_reliability,
    stale_signal_is_too_old,
    validate_candidate,
)
from polarmeter_free_provider_probe import yahoo_chart_change_fields
from polarmeter_news_rss_probe import cause_aware_display_headline, classify_relevance, english_market_context_translation, koreanize_english_headline, market_burden_tone, normalize_items


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

    status, reason = validate_candidate(
        "vix",
        {"status": "ok", "price": 20.66, "changePct": 13.45},
    )
    assert status == "suspect"
    assert reason == "changePct_suspect>12.0"
    assert {'us10y', 'dxy', 'wti', 'gold'}.issubset(US_ACTIVE_MARKET_STALE_KEYS)
    vix_reliability = signal_reliability('vix', 'public-chart-delayed', status, reason)
    assert vix_reliability['displayBadge'] == '변동 큼 · 확인 전'
    assert vix_reliability['confidencePolicy'] == 'low_large_move_until_corroborated'

    now = datetime.now(timezone.utc)
    old_samsung_at = (now - timedelta(hours=120)).isoformat().replace('+00:00', 'Z')
    recent_samsung_at = (now - timedelta(hours=48)).isoformat().replace('+00:00', 'Z')
    assert MAX_STALE_SIGNAL_AGE_HOURS['kr_samsung'] == 96
    assert hard_stale_reason('kr_samsung', 'data-go-kr-free', {'asOf': old_samsung_at}) is not None
    assert hard_stale_reason('kr_samsung', 'data-go-kr-free', {'asOf': recent_samsung_at}) is None
    assert stale_signal_is_too_old('kr_samsung', {'dataAsOf': old_samsung_at})
    assert not stale_signal_is_too_old('kr_samsung', {'dataAsOf': recent_samsung_at})

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
    normalized_wti, _ = normalize_items([{
        "label": "Smoke QA",
        "items": [{
            "headline": wti_relief_headline,
            "sourceName": "Smoke QA",
            "publishedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "url": "https://example.com/wti-down",
        }],
    }], 1)
    assert normalized_wti[0]["headline"] == wti_display
    assert normalized_wti[0]["originalHeadline"] is None

    ai_oil_burden = "AI stocks slump again, while oil prices keep climbing"
    assert market_burden_tone(ai_oil_burden, "neutral") == "negative"
    assert koreanize_english_headline("SPX: S&P 500 Drops 0.2% as AI Worries Strike Again. But Not for Meta.") == "AI 우려 재부각에 S&P500 0.2% 하락, 메타는 예외"
    assert koreanize_english_headline("S&P 500, Nasdaq Futures Climb While Dow Futures Fall Ahead Of Key Jobs Report") == "고용지표 앞두고 S&P500·나스닥 선물 상승, 다우 선물 하락"
    earnings_calendar = "다음주 S&P500 기업중 80개 실적 발표…알파벳·테슬라·인텔 예정"
    assert cause_aware_display_headline(earnings_calendar, earnings_calendar) == "다음주 S&P500 기업 80개 실적 발표 예정"

    published_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    relevance, reason = classify_relevance("연준·아이들, 한터차트로 증명한 '글로벌 존재감'", "연예뉴스", published_at)
    assert relevance is None
    assert reason == "ENTERTAINMENT_HOMONYM_NOT_MARKET_TEMPERATURE"
    relevance, reason = classify_relevance("삼성전자(005930)", "매일경제 마켓", published_at)
    assert relevance is None
    assert reason == "LOW_INFORMATION_QUOTE_HEADLINE"
    relevance, reason = classify_relevance(
        "The VIX Futures Curve Signal That Could Cut SVOL's Yield in Half",
        "Yahoo Finance RSS",
        published_at,
    )
    assert relevance is None
    assert reason == "PERSONAL_FINANCE_NOT_MARKET_TEMPERATURE"
    relevance, reason = classify_relevance(
        "[고래사냥]'한미반도체·케이뱅크! 내일장 고래 종목은?! - 머니투데이",
        "머니투데이",
        published_at,
    )
    assert relevance is None
    assert reason == "INVESTMENT_ACTION_OR_SINGLE_STOCK_NOISE"
    relevance, reason = classify_relevance("연준 로건, 7월 회의 앞두고 금리 인상 촉구", "연합뉴스", published_at)
    assert relevance is not None
    assert reason == "PASS"
    print("PolarMeter provider change smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
