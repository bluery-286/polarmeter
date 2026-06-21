#!/usr/bin/env python3
"""Run the PolarMeter free/delayed data cache pipeline.

This is the local precursor to the cache-server scheduled worker. It keeps the
same safety contract as the app path:
- no paid provider enablement
- no client direct provider calls
- provider keys are read only from server/local secrets or env
- generated app fixture is derived from a normalized snapshot
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
PROJECT = WORKSPACE
TOOLS = WORKSPACE / 'tools'
DEFAULT_REPORT = PROJECT / 'testflight/free-provider-probe-report-latest.json'
DEFAULT_NEWS_REPORT = PROJECT / 'testflight/news-rss-probe-latest.json'
DEFAULT_SNAPSHOT = PROJECT / 'testflight/free-cache-snapshot-latest.json'
DEFAULT_FIXTURE = PROJECT / 'testflight/free-cache-experiment.json'
DEFAULT_LAST_KNOWN_GOOD = PROJECT / 'testflight/last-known-good-snapshot.json'
DEFAULT_PUBLIC_SNAPSHOT_NAME = 'market-snapshot-latest.json'
DEFAULT_PUBLIC_MANIFEST_NAME = 'market-snapshot-manifest.json'
DEFAULT_PUBLIC_HEALTH_NAME = 'health.json'
WEEKDAY_NEWS_TTL_MINUTES = 30
WEEKEND_NEWS_TTL_MINUTES = 60
NEWS_RECOMMENDED_SCHEDULE = '30min_weekdays_60min_weekends_public_headline_cache'


def run(cmd: list[str], *, stdout_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    if stdout_path:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open('w', encoding='utf-8') as out:
            return subprocess.run(cmd, cwd=WORKSPACE, text=True, stdout=out, stderr=subprocess.PIPE, check=True)
    return subprocess.run(cmd, cwd=WORKSPACE, text=True, capture_output=True, check=True)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write('\n')
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def recommended_news_ttl_minutes(now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    return WEEKEND_NEWS_TTL_MINUTES if current.weekday() >= 5 else WEEKDAY_NEWS_TTL_MINUTES


def assert_contract(report: dict[str, Any], snapshot: dict[str, Any], fixture: dict[str, Any]) -> None:
    if report.get('paidProviderEnabled') is not False:
        raise AssertionError('provider report must keep paidProviderEnabled=false')
    if report.get('clientDirectProviderCalls') is not False:
        raise AssertionError('provider report must keep clientDirectProviderCalls=false')
    if snapshot.get('paidProviderEnabled') is not False:
        raise AssertionError('snapshot must keep paidProviderEnabled=false')
    if snapshot.get('clientDirectProviderCalls') is not False:
        raise AssertionError('snapshot must keep clientDirectProviderCalls=false')
    if snapshot.get('status') not in {'ok', 'partial', 'needs_keys'}:
        raise AssertionError(f"unexpected snapshot status: {snapshot.get('status')}")
    quality = snapshot.get('dataQuality') or {}
    if quality.get('policy') != 'core_signal_fallback_chain_v1':
        raise AssertionError('snapshot must include core signal fallback dataQuality policy')
    if not isinstance(quality.get('coreCoverageRatio'), (int, float)):
        raise AssertionError('snapshot dataQuality must include coreCoverageRatio')

    required = {'sp500', 'nasdaq100', 'vix', 'usd_krw', 'wti', 'soxx', 'smh', 'kospi', 'kosdaq', 'kodex200', 'tiger200'}
    missing = required - set(snapshot.get('signals', {}).keys())
    if missing:
        raise AssertionError(f'missing required snapshot signals: {sorted(missing)}')

    for screen_key, screen in fixture.items():
        if not isinstance(screen, dict):
            continue
        if screen.get('source') != 'cached':
            raise AssertionError(f'{screen_key} source must be cached')
        if screen.get('badge') != 'delayed':
            raise AssertionError(f'{screen_key} badge must be delayed')
        policy = screen.get('_cachePolicy') or {}
        if policy.get('paidProviderEnabled') is not False:
            raise AssertionError(f'{screen_key} paid provider policy must be false')
        if policy.get('clientDirectProviderCalls') is not False:
            raise AssertionError(f'{screen_key} client direct provider calls must be false')
        refs = list((screen.get('_meta') or {}).get('sourceRefs') or []) + list((screen.get('sources') or {}).keys())
        bad_refs = [ref for ref in refs if isinstance(ref, str) and ref.startswith(('yahoo:', 'naver:'))]
        if bad_refs:
            raise AssertionError(f'{screen_key} has unlicensed refs in free-cache mode: {bad_refs[:3]}')

    news = snapshot.get('news') or {}
    if news.get('paidProviderEnabled') is not False or news.get('clientDirectProviderCalls') is not False:
        raise AssertionError('cached news must keep paid/client-direct policies false')
    if news.get('bodyScrapingEnabled') is not False or news.get('imageScrapingEnabled') is not False:
        raise AssertionError('cached news must not include body/image scraping')
    for item in news.get('items') or []:
        if item.get('body') or item.get('imageUrl'):
            raise AssertionError('cached news item must not contain article body or imageUrl')
        if item.get('displayHeadline') and re_has_english_only(item.get('displayHeadline')):
            raise AssertionError('cached news displayHeadline must be Korean-first for user-facing cards')
        if not item.get('categoryLabel') or not item.get('whyImportant') or item.get('scoreAnchor') != 'market_temperature_context':
            raise AssertionError('cached news item must explain category/market-temperature evidence anchor')
        if not item.get('relatedFactors'):
            raise AssertionError('cached news item must expose related score factors')


def re_has_english_only(value: Any) -> bool:
    text = str(value or '')
    return bool(text.strip()) and not any('\uac00' <= ch <= '\ud7a3' for ch in text) and any(('a' <= ch.lower() <= 'z') for ch in text)


def public_manifest(snapshot: dict[str, Any], snapshot_name: str) -> dict[str, Any]:
    signals = snapshot.get('signals', {})
    ok_signals = sorted(k for k, v in signals.items() if isinstance(v, dict) and v.get('status') == 'ok')
    blocked_signals = sorted(k for k, v in signals.items() if isinstance(v, dict) and v.get('status') not in {'ok', None})
    news = snapshot.get('news') or {}
    return {
        'mode': snapshot.get('mode') or 'free_cache_experiment',
        'generatedAt': snapshot.get('generatedAt'),
        'snapshotPath': snapshot_name,
        'snapshotStatus': snapshot.get('status'),
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'cacheControl': 'public, max-age=300, stale-while-revalidate=1800',
        'okSignals': ok_signals,
        'okNewsCount': len(news.get('items') or []),
        'newsStatus': news.get('status') or 'unavailable',
        'newsTtlMinutes': news.get('ttlMinutes') or recommended_news_ttl_minutes(),
        'newsNextRefreshAt': news.get('nextRefreshAt'),
        'newsRecommendedSchedule': news.get('recommendedSchedule') or NEWS_RECOMMENDED_SCHEDULE,
        'dataQuality': snapshot.get('dataQuality') or {},
        'blockedOrUnavailableSignals': blocked_signals,
        'notes': [
            'Public artifact contains normalized delayed/free data only.',
            'Provider API keys and raw provider URLs must never be published.',
        ],
    }


def public_health(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        'ok': manifest.get('snapshotStatus') in {'ok', 'partial'},
        'generatedAt': manifest.get('generatedAt'),
        'snapshotStatus': manifest.get('snapshotStatus'),
        'okSignalCount': len(manifest.get('okSignals') or []),
        'okNewsCount': manifest.get('okNewsCount') or 0,
        'newsStatus': manifest.get('newsStatus') or 'unavailable',
        'dataQuality': manifest.get('dataQuality') or {},
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
    }


def sanitize_public_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """Return the app-facing subset of a normalized signal.

    Public cache artifacts must be enough for the app to render source/freshness
    state, but must not expose worker internals such as fallback chains, missing
    secret names, raw provider diagnostics, or probe errors.
    """
    allowed = {
        'key', 'label', 'value', 'change', 'changePct', 'status', 'freshnessStatus',
        'provider', 'sourceId', 'fetchedAt', 'dataAsOf', 'ttlMinutes', 'valuePolicy',
        'licenseNote', 'coreSignal', 'qualityStatus', 'reliability', 'lastSuccessfulAt',
        'staleSource',
    }
    return {key: value for key, value in signal.items() if key in allowed}


def sanitize_public_news(news: dict[str, Any]) -> dict[str, Any]:
    allowed_news = {
        'status', 'generatedAt', 'ttlMinutes', 'nextRefreshAt', 'recommendedSchedule',
        'sourcePolicy', 'bodyScrapingEnabled', 'imageScrapingEnabled',
        'paidProviderEnabled', 'clientDirectProviderCalls', 'items',
    }
    allowed_item = {
        'headline', 'displayHeadline', 'originalHeadline', 'sourceName', 'publishedAt',
        'language', 'translationNote',
        'url', 'impactTarget', 'impactTone', 'category', 'categoryLabel', 'tags',
        'relatedFactors', 'whyImportant', 'scoreAnchor', 'qualityScore', 'priorityTier',
        'critical', 'criticalReason', 'sourceId', 'region', 'provider', 'licenseNote',
    }
    out = {key: value for key, value in news.items() if key in allowed_news and key != 'items'}
    out['paidProviderEnabled'] = False
    out['clientDirectProviderCalls'] = False
    out['bodyScrapingEnabled'] = False
    out['imageScrapingEnabled'] = False
    out['items'] = [
        {key: value for key, value in item.items() if key in allowed_item}
        for item in news.get('items') or []
        if isinstance(item, dict)
    ]
    return out


def sanitize_public_data_quality(data_quality: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        'policy', 'coreSignals', 'coreGroups', 'coreOkSignals', 'coreCoverageRatio',
        'normalTemperatureAllowed', 'displayMode', 'groupStatus', 'displayBadge',
    }
    return {key: value for key, value in data_quality.items() if key in allowed}


def sanitize_public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        'mode': snapshot.get('mode') or 'free_cache_experiment',
        'generatedAt': snapshot.get('generatedAt'),
        'status': snapshot.get('status'),
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'defaultTtlMinutes': snapshot.get('defaultTtlMinutes') or 30,
        'dataQuality': sanitize_public_data_quality(snapshot.get('dataQuality') or {}),
        'signals': {
            key: sanitize_public_signal(value)
            for key, value in (snapshot.get('signals') or {}).items()
            if isinstance(value, dict)
        },
        'news': sanitize_public_news(snapshot.get('news') or {}),
    }


def assert_public_payload_safe(snapshot: dict[str, Any]) -> None:
    raw = json.dumps(snapshot, ensure_ascii=False).lower()
    forbidden_tokens = [
        'api_key', 'apikey=', 'servicekey=', 'service_key', 'secret',
        'twelve_data_api_key', 'fmp_api_key', 'data_go_kr_service_key',
        'bok_api_key', 'ecos_api_key', 'missing_key', 'feedresults',
        'fallbackchain', 'qualityreason', 'marketimpactcomponents',
    ]
    leaked = [token for token in forbidden_tokens if token in raw]
    if leaked:
        raise AssertionError(f'public snapshot leaked internal token(s): {leaked}')
    news = snapshot.get('news') or {}
    for item in news.get('items') or []:
        if item.get('body') or item.get('imageUrl') or item.get('description'):
            raise AssertionError('public news item must not expose body/image/description fields')


def publish_public_artifacts(public_dir: Path, snapshot: dict[str, Any], snapshot_name: str) -> dict[str, str]:
    snapshot_path = public_dir / snapshot_name
    manifest_path = public_dir / DEFAULT_PUBLIC_MANIFEST_NAME
    health_path = public_dir / DEFAULT_PUBLIC_HEALTH_NAME
    public_snapshot = sanitize_public_snapshot(snapshot)
    assert_public_payload_safe(public_snapshot)
    manifest = public_manifest(public_snapshot, snapshot_name)
    atomic_write_json(snapshot_path, public_snapshot)
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(health_path, public_health(manifest))
    return {
        'snapshot': str(snapshot_path),
        'manifest': str(manifest_path),
        'health': str(health_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, default=DEFAULT_REPORT)
    parser.add_argument('--news-report', type=Path, default=DEFAULT_NEWS_REPORT)
    parser.add_argument('--snapshot', type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument('--fixture', type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument('--last-known-good', type=Path, default=DEFAULT_LAST_KNOWN_GOOD)
    parser.add_argument('--skip-fixture', action='store_true', help='Only update provider report and normalized snapshot')
    parser.add_argument('--public-dir', type=Path, help='Write deployable public JSON artifacts into this directory')
    parser.add_argument('--public-snapshot-name', default=DEFAULT_PUBLIC_SNAPSHOT_NAME)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    run([sys.executable, str(TOOLS / 'polarmeter_free_provider_probe.py'), '--json'], stdout_path=args.report)
    news_ttl_minutes = recommended_news_ttl_minutes()
    run([sys.executable, str(TOOLS / 'polarmeter_news_rss_probe.py'), '--output', str(args.news_report), '--ttl-minutes', str(news_ttl_minutes)])
    run([sys.executable, str(TOOLS / 'polarmeter_cache_snapshot.py'), '--probe', str(args.report), '--news-probe', str(args.news_report), '--output', str(args.snapshot), '--last-known-good', str(args.last_known_good)])
    if not args.skip_fixture:
        run([sys.executable, str(TOOLS / 'polarmeter_free_cache_fixture.py'), '--snapshot', str(args.snapshot), '--output', str(args.fixture)])

    report = load_json(args.report)
    snapshot = load_json(args.snapshot)
    fixture = load_json(args.fixture) if args.fixture.exists() and not args.skip_fixture else {}
    if not args.skip_fixture:
        assert_contract(report, snapshot, fixture)
    public_artifacts = None
    if args.public_dir:
        public_artifacts = publish_public_artifacts(args.public_dir, snapshot, args.public_snapshot_name)

    summary = {
        'ok': True,
        'report': str(args.report),
        'newsReport': str(args.news_report),
        'snapshot': str(args.snapshot),
        'fixture': None if args.skip_fixture else str(args.fixture),
        'reportStatus': report.get('status'),
        'snapshotStatus': snapshot.get('status'),
        'okSignals': sorted(k for k, v in snapshot.get('signals', {}).items() if v.get('status') == 'ok'),
        'coreCoverageRatio': (snapshot.get('dataQuality') or {}).get('coreCoverageRatio'),
        'displayMode': (snapshot.get('dataQuality') or {}).get('displayMode'),
        'okNewsCount': len((snapshot.get('news') or {}).get('items') or []),
        'publicArtifacts': public_artifacts,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"PolarMeter free-cache worker: PASS report={summary['reportStatus']} snapshot={summary['snapshotStatus']} okSignals={len(summary['okSignals'])}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
