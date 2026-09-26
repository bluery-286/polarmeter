#!/usr/bin/env python3
"""Regression contract: production cache generation must not hide a failed worker."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polarmeter_cache_snapshot as cache
import polarmeter_free_cache_worker as worker
import polarmeter_github_pages_prepare as prepare
import polarmeter_github_pages_smoke as pages_smoke


WORKSPACE = Path(__file__).resolve().parent.parent
WORKFLOW = WORKSPACE / '.github/workflows/polarmeter-cache-pages.yml'


def public_payload(now: datetime, age: timedelta, *, news_count: int = 10) -> dict[str, str]:
    snapshot = {
        'status': 'partial',
        'generatedAt': (now - age).isoformat().replace('+00:00', 'Z'),
        'dataQuality': {'coreCoverageRatio': 0.8, 'displayMode': 'normal'},
        'news': {'items': [{} for _ in range(news_count)]},
        'temperatureHistory': {
            'version': 'temperature-history-v1',
            'retentionDays': 7,
            'items': [],
            'dailyDelta': {'status': 'ready'},
        },
    }
    manifest = {'snapshotStatus': 'partial', 'okNewsCount': news_count}
    return {
        'market-snapshot-latest.json': json.dumps(snapshot),
        'market-snapshot-manifest.json': json.dumps(manifest),
        'health.json': json.dumps({'ok': True}),
    }


def main() -> None:
    cpi_preview = 'Nasdaq 100: Tech Stocks Lead Monday’s Pre-Market Bid Ahead of CPI'
    assert not pages_smoke.is_expired_cpi_preview(
        cpi_preview,
        '2026-08-10T10:18:00Z',
        '2026-08-12T12:30:00Z',
    )
    assert pages_smoke.is_expired_cpi_preview(
        cpi_preview,
        '2026-08-12T12:31:00Z',
        '2026-08-12T12:30:00Z',
    )
    assert not pages_smoke.is_expired_cpi_preview(
        'Nasdaq 100 rises as chip stocks rebound',
        '2026-08-12T12:31:00Z',
        '2026-08-12T12:30:00Z',
    )

    now = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
    shortfall_failures = [
        {'check': 'snapshot.news.items', 'reason': 'below_minimum'},
        {'check': 'manifest.okNewsCount', 'reason': 'below_minimum'},
    ]
    recent_fallback = public_payload(now, timedelta(hours=1, minutes=59))
    with TemporaryDirectory(prefix='polarmeter-recent-news-fallback-') as tmp:
        output_dir = Path(tmp)
        assert prepare.reuse_recent_public_payload_for_news_shortfall(
            output_dir, recent_fallback, shortfall_failures, now=now,
        )
        restored = json.loads((output_dir / 'market-snapshot-latest.json').read_text())
        assert restored['generatedAt'] == json.loads(recent_fallback['market-snapshot-latest.json'])['generatedAt']
        assert prepare.recent_publishable_public_payload(
            public_payload(now, timedelta(hours=2)), now=now,
        ), 'a snapshot exactly 2 hours old must remain eligible'
        assert not prepare.recent_publishable_public_payload(
            public_payload(now, timedelta(hours=2, seconds=1)), now=now,
        ), 'snapshots older than 2 hours must be rejected'
        assert not prepare.recent_publishable_public_payload(
            public_payload(now, timedelta(minutes=5), news_count=9), now=now,
        ), 'fallback must still satisfy the 10-news minimum'
        assert not prepare.reuse_recent_public_payload_for_news_shortfall(
            output_dir,
            recent_fallback,
            shortfall_failures + [{'check': 'health.ok', 'reason': 'value_mismatch'}],
            now=now,
        ), 'non-news payload failures must never use the shortfall fallback'

    live_now = datetime.now(timezone.utc)
    live_fallback = public_payload(live_now, timedelta(minutes=30))
    with TemporaryDirectory(prefix='polarmeter-main-news-fallback-') as tmp:
        output = io.StringIO()
        with (
            patch.object(prepare, 'copy_site'),
            patch.object(prepare, 'read_public_files', return_value=live_fallback),
            patch.object(prepare, 'read_remote_public_files', return_value=live_fallback),
            patch.object(prepare, 'seed_last_known_good_from_site', return_value=False),
            patch.object(
                prepare,
                'run_worker_with_publishability_retries',
                return_value=({'freshnessAudit': 'passed'}, shortfall_failures),
            ),
            patch.object(prepare, 'assert_pages_contract'),
            patch.object(sys, 'argv', [
                'polarmeter_github_pages_prepare.py', '--output', tmp, '--json',
                '--news-shortfall-retries', '1', '--news-shortfall-retry-delay-seconds', '0',
            ]),
            redirect_stdout(output),
        ):
            assert prepare.main() == 0
        result = json.loads(output.getvalue())
        assert result['ok'] is True
        assert result['usedExistingPublicSnapshot'] is True
        assert result['newsShortfallFallbackUsed'] is True
        assert result['okNewsCount'] == 10

    now = datetime.now(timezone.utc)
    signal = {
        'key': 'kospi',
        'label': 'KOSPI',
        'providerSymbol': {'yahoo_chart': '^KS11', 'data_go_kr_index': '코스피'},
        'category': 'kr_index',
    }
    providers = {
        'public-chart-delayed': [{
            'key': 'kospi',
            'symbol': '^KS11',
            'status': 'ok',
            'price': 6595.45,
            'changePct': 17.9,
            'asOf': int((now - timedelta(hours=26)).timestamp()),
        }],
        'data-go-kr-index-free': [{
            'key': 'kospi',
            'symbol': '코스피',
            'status': 'ok',
            'price': 5593.56,
            'changePct': -1.23,
            'asOf': int((now - timedelta(hours=60)).timestamp()),
        }],
    }
    # This contract isolates candidate ordering from the separate live-market
    # staleness guards. Without the isolation, the same fixed 26h/60h fixture
    # becomes stale only during Korea market hours and makes CI time-dependent.
    with (
        patch.object(cache, 'kr_intraday_stale_reason', return_value=None),
        patch.object(cache, 'active_market_stale_reason', return_value=None),
        patch.object(cache, 'hard_stale_reason', return_value=None),
    ):
        selected = cache.choose_signal(signal, providers, {})
    assert selected['provider'] == 'public-chart-delayed'
    assert selected['status'] == 'suspect'
    assert selected['valuePolicy'] == 'show'
    assert str((selected.get('reliability') or {}).get('confidencePolicy') or '').startswith('low_')

    audit_failure = subprocess.CalledProcessError(
        1,
        ['polarmeter_data_freshness_audit.py'],
        output='PolarMeter data freshness audit: FAIL\n- kospi: synthetic freshness failure',
        stderr='',
    )
    with patch.object(worker, 'run', side_effect=audit_failure):
        try:
            worker.run_freshness_audit(Path('snapshot.json'), Path('probe.json'))
        except RuntimeError as error:
            assert 'kospi: synthetic freshness failure' in str(error)
        else:
            raise AssertionError('freshness audit failure details were hidden')

    fallback_files = {
        'market-snapshot-latest.json': '{}',
        'market-snapshot-manifest.json': '{}',
        'health.json': '{}',
    }
    with TemporaryDirectory(prefix='polarmeter-strict-failure-') as tmp:
        with (
            patch.object(prepare, 'copy_site'),
            patch.object(prepare, 'read_public_files', return_value=fallback_files),
            patch.object(prepare, 'read_remote_public_files', return_value=fallback_files),
            patch.object(prepare, 'restore_public_files'),
            patch.object(prepare, 'seed_last_known_good_from_site', return_value=True),
            patch.object(prepare, 'run_worker', side_effect=RuntimeError('synthetic worker failure')),
            patch.object(sys, 'argv', ['polarmeter_github_pages_prepare.py', '--output', tmp]),
        ):
            try:
                prepare.main()
            except RuntimeError as error:
                assert 'synthetic worker failure' in str(error)
            else:
                raise AssertionError('production prepare path hid a worker failure behind stale public data')

    workflow = WORKFLOW.read_text(encoding='utf-8')
    assert "'6,21,36,51 12,13,14 * * 1-5'" in workflow
    assert "'6,21,36,51 18,19 * * 1-5'" in workflow
    assert 'macro_watch_schedules' in workflow
    assert 'CACHE_SNAPSHOT_URL' in workflow
    assert 'python3 tools/polarmeter_github_pages_prepare.py ' in workflow
    assert '--allow-stale-fallback' not in workflow
    assert '--news-shortfall-retries 1' in workflow
    assert '--news-shortfall-retry-delay-seconds 45' in workflow
    assert '--news-shortfall-fallback-max-age-minutes 120' in workflow
    assert '"$RUNNER_TEMP/polarmeter-site"' in workflow
    assert 'x-access-token:' not in workflow
    assert 'git push --force origin HEAD:gh-pages' in workflow
    print('PASS — worker failure is fatal and official macro release watch is scheduled')


if __name__ == '__main__':
    main()
