#!/usr/bin/env python3
"""Regression contract: production cache generation must not hide a failed worker."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polarmeter_cache_snapshot as cache
import polarmeter_free_cache_worker as worker
import polarmeter_github_pages_prepare as prepare


WORKSPACE = Path(__file__).resolve().parent.parent
WORKFLOW = WORKSPACE / '.github/workflows/polarmeter-cache-pages.yml'


def main() -> None:
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
    build_line = next(
        line for line in workflow.splitlines()
        if 'python3 tools/polarmeter_github_pages_prepare.py --output ./_site' in line
    )
    assert '--allow-stale-fallback' not in build_line
    print('PASS — worker failure is fatal and official macro release watch is scheduled')


if __name__ == '__main__':
    main()
