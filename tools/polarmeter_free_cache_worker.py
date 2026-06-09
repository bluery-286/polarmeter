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
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
PROJECT = WORKSPACE
TOOLS = WORKSPACE / 'tools'
DEFAULT_REPORT = WORKSPACE / 'testflight/free-provider-probe-report-latest.json'
DEFAULT_NEWS_REPORT = WORKSPACE / 'testflight/news-rss-probe-latest.json'
DEFAULT_SNAPSHOT = WORKSPACE / 'testflight/free-cache-snapshot-latest.json'
DEFAULT_FIXTURE = WORKSPACE / 'testflight/free-cache-experiment.json'
DEFAULT_PUBLIC_SNAPSHOT_NAME = 'market-snapshot-latest.json'
DEFAULT_PUBLIC_MANIFEST_NAME = 'market-snapshot-manifest.json'
DEFAULT_PUBLIC_HEALTH_NAME = 'health.json'


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
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
    }


def publish_public_artifacts(public_dir: Path, snapshot: dict[str, Any], snapshot_name: str) -> dict[str, str]:
    snapshot_path = public_dir / snapshot_name
    manifest_path = public_dir / DEFAULT_PUBLIC_MANIFEST_NAME
    health_path = public_dir / DEFAULT_PUBLIC_HEALTH_NAME
    manifest = public_manifest(snapshot, snapshot_name)
    atomic_write_json(snapshot_path, snapshot)
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
    parser.add_argument('--skip-fixture', action='store_true', help='Only update provider report and normalized snapshot')
    parser.add_argument('--public-dir', type=Path, help='Write deployable public JSON artifacts into this directory')
    parser.add_argument('--public-snapshot-name', default=DEFAULT_PUBLIC_SNAPSHOT_NAME)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    run([sys.executable, str(TOOLS / 'polarmeter_free_provider_probe.py'), '--json'], stdout_path=args.report)
    run([sys.executable, str(TOOLS / 'polarmeter_news_rss_probe.py'), '--output', str(args.news_report)])
    run([sys.executable, str(TOOLS / 'polarmeter_cache_snapshot.py'), '--probe', str(args.report), '--news-probe', str(args.news_report), '--output', str(args.snapshot)])
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
