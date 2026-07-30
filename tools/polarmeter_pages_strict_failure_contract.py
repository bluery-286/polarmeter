#!/usr/bin/env python3
"""Regression contract: production cache generation must not hide a failed worker."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polarmeter_github_pages_prepare as prepare


WORKSPACE = Path(__file__).resolve().parent.parent
WORKFLOW = WORKSPACE / '.github/workflows/polarmeter-cache-pages.yml'


def main() -> None:
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
