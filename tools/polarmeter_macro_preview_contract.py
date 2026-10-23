#!/usr/bin/env python3
"""Offline regression for the RSS/Pages CPI-preview expiry boundary."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polarmeter_cache_snapshot as cache
import polarmeter_github_pages_smoke as pages
import polarmeter_news_rss_probe as rss
from polarmeter_macro_preview_policy import expired_us_cpi_preview, parse_timestamp

FAILED_TITLE = 'US CPI Preview: Follow the Fed and Look Past the Hot Headline'


def public_fixture(current: datetime, headline: str, published: str, status: str) -> tuple[dict, dict, dict]:
    due = [(parse_timestamp(at), label) for at, label in cache.SCHEDULED_MACRO_EVENTS['us_cpi'] if parse_timestamp(at) <= current - cache.MACRO_OFFICIAL_FETCH_DELAY]
    upcoming = next(((parse_timestamp(at), label) for at, label in cache.SCHEDULED_MACRO_EVENTS['us_cpi'] if parse_timestamp(at) > current), None)
    last = due[-1] if status == 'ok' else due[-2]
    next_release = upcoming if status == 'ok' and upcoming else due[-1]
    generated = current.isoformat()
    item = {
        'headline': headline, 'displayHeadline': headline, 'originalHeadline': headline,
        'publishedAt': published, 'categoryLabel': '매크로', 'whyImportant': '시장 배경 확인',
        'scoreAnchor': 'market_temperature_context', 'issueClusterKey': 'fixture',
    }
    snapshot = {
        'generatedAt': generated, 'paidProviderEnabled': False, 'clientDirectProviderCalls': False,
        'temperatureHistory': {'version': 'temperature-history-v1', 'retentionDays': 7, 'items': [], 'dailyDelta': {'status': 'ready'}},
        'dataQuality': {'coreCoverageRatio': 1.0, 'displayMode': 'normal'}, 'signals': {},
        'macroEvents': {'us_cpi': {
            'status': status, 'sourcePolicy': 'official_release_registry_with_expiry_gate',
            'lastRelease': {'label': last[1], 'releasedAt': last[0].isoformat(), 'resultLabel': 'fixture', 'detail': 'fixture', 'sourceLabel': 'BLS', 'sourceUrl': 'https://www.bls.gov/news.release/cpi.nr0.htm'},
            'nextRelease': {'label': next_release[1], 'scheduledAt': next_release[0].isoformat()},
        }},
        'news': {'paidProviderEnabled': False, 'clientDirectProviderCalls': False, 'bodyScrapingEnabled': False, 'imageScrapingEnabled': False,
                 'items': [dict(item) for _ in range(10)], 'ttlMinutes': 30, 'nextRefreshAt': generated},
        'defaultTtlMinutes': 60, 'nextRefreshAt': generated,
        'refreshPolicy': {'version': 'market-aware-cache-refresh-v1'},
        'costGuardrails': {'killSwitchStatus': {'rapidMoveBriefingGeneration': 'ready'}},
    }
    manifest = {
        'snapshotPath': 'market-snapshot-latest.json', 'snapshotStatus': 'partial', 'okNewsCount': 10,
        'newsTtlMinutes': 30, 'newsNextRefreshAt': generated, 'marketDataTtlMinutes': 60,
        'marketDataNextRefreshAt': generated, 'nextRefreshAt': generated,
        'newsRecommendedSchedule': '30min_weekdays_60min_weekends_public_headline_cache',
        'marketDataRecommendedSchedule': 'market_aware_60min_daily_kr_us_open_close_confirmations',
        'criticalMarketRefreshes': [{'key': key} for key in (
            'kr_open_plus_30', 'kr_open_plus_60', 'kr_close_plus_15', 'kr_close_plus_60',
            'us_open_plus_5_dst', 'us_open_plus_30', 'us_open_plus_60', 'us_close_plus_15', 'us_close_plus_60',
        )],
        'dataServingMode': 'limited', 'lastSuccessfulSnapshotAt': generated,
        'providerCallCount': 0, 'providerFailureCount': 0, 'providerStatusByName': {},
        'estimatedMonthlyCost': {}, 'budgetLimit': {'monthlyLimit': 50, 'warningLimit': 20},
        'killSwitchStatus': {}, 'costGuardrails': {'commercialUseChecked': False},
        'budgetCapConfigured': True, 'commercialUseChecked': False, 'killSwitchActive': False,
        'temperatureHistoryStatus': 'ready',
    }
    health = dict(manifest)
    return snapshot, manifest, health


def check_payload(current: datetime, headline: str, published: str, status: str, expired: bool) -> None:
    snapshot, manifest, health = public_fixture(current, headline, published, status)
    with TemporaryDirectory(prefix='cpi-preview-contract-') as tmp:
        out = Path(tmp)
        for name in pages.REQUIRED_FILES:
            (out / name).write_text('', encoding='utf-8')
        for name, payload in zip(
            ('market-snapshot-latest.json', 'market-snapshot-manifest.json', 'health.json'),
            (snapshot, manifest, health),
        ):
            (out / name).write_text(json.dumps(payload), encoding='utf-8')
        try:
            pages.validate_payload(out)
        except AssertionError as error:
            assert expired and 'must not keep an expired CPI preview' in str(error), error
        else:
            assert not expired, 'Pages admitted a stale preview'


def main() -> None:
    checks = 0

    def check(headline: str, published: object, current: datetime, expected: bool) -> None:
        nonlocal checks
        assert expired_us_cpi_preview(headline, published, current) is expected, (headline, published, current)
        assert rss.expired_macro_event_preview(headline, published, now=current) is expected
        assert pages.is_expired_cpi_preview(headline, current.isoformat(), published) is expected
        checks += 3

    # Exact failed run: newly stamped generic preview, well after the Oct release.
    october = datetime(2026, 10, 22, 18, 45, tzinfo=timezone.utc)
    check(FAILED_TITLE, '2026-10-22T10:05:42Z', october, True)
    # The calendar, not an August constant, continues to roll every month.
    for at, _ in cache.SCHEDULED_MACRO_EVENTS['us_cpi']:
        release = parse_timestamp(at)
        for lead, expired in ((timedelta(days=3, seconds=1), True), (timedelta(days=3), False), (timedelta(seconds=1), False)):
            current = release - lead
            check(FAILED_TITLE, (current - timedelta(hours=1)).isoformat(), current, expired)
        for lag in (timedelta(0), timedelta(seconds=1), timedelta(hours=2)):
            current = release + lag
            check(FAILED_TITLE, (release - timedelta(hours=1)).isoformat(), current, True)

    november = datetime(2026, 11, 8, 13, 30, tzinfo=timezone.utc)
    published = (november - timedelta(hours=1)).isoformat()
    check('미국 10월 CPI 발표 앞두고 나스닥 선물 대기', published, november, False)
    check('US October CPI preview', published, november, False)
    check('US CPI preview for October', published, november, False)
    check('US October 2026 CPI preview', published, november, False)
    check('US 2026 October CPI preview', published, november, False)
    check('US CPI preview for October 2026', published, november, False)
    check('US October 2025 CPI preview', published, november, True)
    check('US 2025 October CPI preview', published, november, True)
    check('US CPI preview for October 2025', published, november, True)
    check('US CPI preview for 2025 October', published, november, True)
    check('미국 2025년 10월 CPI 발표 예고', published, november, True)
    check('미국 2026년 10월 CPI 발표 예고', published, november, False)
    check('US September CPI preview', published, november, True)
    check('미국 9월 소비자물가 발표 예고', published, november, True)
    check(FAILED_TITLE, '2026-10-14T12:29:59Z', november, True)
    for invalid in (None, '', 'not-a-date', '2026-11-08T10:00:00', '2026-11-09T10:00:00Z'):
        check(FAILED_TITLE, invalid, november, True)
    for title in ('US CPI comes in below expectations', '미국 CPI 예상 하회…금리 부담 완화', 'Nasdaq rises as chip stocks rebound', 'UK CPI preview', '중국 소비자물가 발표 앞두고 증시 대기'):
        check(title, '2026-10-22T10:05:42Z', october, False)

    # Review regressions: comparisons, possessives, other countries/metrics,
    # factual results before a different event, and the calendar horizon.
    regressions = [
        ('US October CPI Preview: What September CPI means for markets', published, november, False),
        ("US September's CPI preview", published, november, True),
        ('US September’s CPI preview', published, november, True),
        ("US October's CPI preview", published, november, False),
        ('US October’s CPI preview', published, november, False),
        ('US October 2025 CPI Preview: What September CPI means', published, november, True),
    ]
    for country in ('India', 'Brazil', 'France', 'Mexico', 'South Africa'):
        regressions.extend([
            (f'{country} CPI preview ahead of release', '2026-10-22T10:05:42Z', october, False),
            (f'US markets await {country} CPI release', '2026-10-22T10:05:42Z', october, False),
        ])
    for title in (
        'US PCE inflation report preview ahead of Friday release',
        'US PPI inflation data preview', '미국 생산자물가 발표 앞두고 증시 대기',
        'US CPI cools to 2.5% ahead of the Fed meeting',
        'US CPI beats estimates before Fed decision',
        '미국 CPI 2.5% 발표…연준 회의 앞두고 금리 하락',
        'US markets brace for India CPI release', 'Inflation report preview',
    ):
        regressions.append((title, '2026-10-22T10:05:42Z', october, False))
    for title in ('US markets await US CPI release', 'US markets brace for upcoming US CPI release',
                  'US markets ahead of the US CPI report', '미국 CPI 발표 앞두고 관망'):
        regressions.append((title, '2026-10-22T10:05:42Z', october, True))
    for current in (datetime(2026, 12, 11, tzinfo=timezone.utc), datetime(2027, 1, 10, tzinfo=timezone.utc)):
        for title in (FAILED_TITLE, 'US December 2026 CPI preview', 'US January 2027 CPI preview'):
            regressions.append((title, (current - timedelta(hours=1)).isoformat(), current, False))
        regressions.append((FAILED_TITLE, '2026-12-10T12:00:00Z', current, True))
    for title, stamp, current, expired in regressions:
        check(title, stamp, current, expired)
        for status in ('ok', 'awaiting_official'):
            check_payload(current, title, stamp, status, expired)
            checks += 1

    # Metadata delayed at BLS must not move the news boundary backward.
    for status in ('ok', 'awaiting_official'):
        check_payload(october, FAILED_TITLE, '2026-10-22T10:05:42Z', status, True)
        check_payload(october, 'US CPI comes in below expectations', '2026-10-22T10:05:42Z', status, False)
        check_payload(november, 'US October CPI preview', published, status, False)
        check_payload(november, 'US September CPI preview', published, status, True)
        checks += 4

    # Exercise the actual ingress, not just the shared helper. No network calls.
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return october.astimezone(tz) if tz is not None else october.replace(tzinfo=None)

    with patch.object(rss, 'datetime', FrozenDateTime):
        assert rss.classify_relevance(FAILED_TITLE, 'Reuters', '2026-10-22T10:05:42Z') == (None, 'EXPIRED_MACRO_EVENT_PREVIEW')
        items, summary = rss.normalize_items([{'label': 'fixture', 'items': [{
            'headline': FAILED_TITLE, 'sourceName': 'Reuters', 'publishedAt': '2026-10-22T10:05:42Z',
            'url': 'https://example.com/cpi-preview-fixture',
        }]}], 30)
        assert items == [], items
        assert 'EXPIRED_MACRO_EVENT_PREVIEW' in str(summary), summary
        checks += 3
    print(f'PASS — shared rolling CPI-preview policy: {checks} checks; RSS ingress and Pages ok/awaiting states')


if __name__ == '__main__':
    main()
