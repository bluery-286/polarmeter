#!/usr/bin/env python3
"""Smoke-test the GitHub Pages payload builder in an isolated temp directory."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
TOOLS = WORKSPACE / 'tools'


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='polarmeter-gh-pages-') as tmp:
        out = Path(tmp) / 'pages'
        result = subprocess.run(
            [sys.executable, str(TOOLS / 'polarmeter_github_pages_prepare.py'), '--output', str(out), '--json'],
            cwd=WORKSPACE,
            text=True,
            capture_output=True,
            check=True,
        )
        summary = json.loads(result.stdout)
        if not summary.get('ok'):
            raise AssertionError('pages prepare summary not ok')
        required = ['index.html', 'privacy.html', 'terms.html', 'support.html', 'styles.css', '.nojekyll', 'market-snapshot-latest.json', 'market-snapshot-manifest.json', 'health.json']
        for name in required:
            if not (out / name).exists():
                raise AssertionError(f'missing pages payload file: {name}')
        manifest = json.loads((out / 'market-snapshot-manifest.json').read_text(encoding='utf-8'))
        snapshot = json.loads((out / 'market-snapshot-latest.json').read_text(encoding='utf-8'))
        if manifest.get('snapshotPath') != 'market-snapshot-latest.json':
            raise AssertionError('manifest snapshotPath mismatch')
        raw = '\n'.join((out / name).read_text(encoding='utf-8') for name in required if name.endswith(('.json', '.html', '.css')))
        forbidden = ['TWELVE_DATA_API_KEY', 'FMP_API_KEY', 'DATA_GO_KR_SERVICE_KEY', 'apikey=', 'serviceKey=', 'api.twelvedata.com', 'financialmodelingprep.com', 'apis.data.go.kr']
        leaked = [token for token in forbidden if token in raw]
        if leaked:
            raise AssertionError(f'public snapshot leaked forbidden token(s): {leaked}')
        if snapshot.get('paidProviderEnabled') is not False or snapshot.get('clientDirectProviderCalls') is not False:
            raise AssertionError('snapshot policy fields must be false')
    print('PolarMeter GitHub Pages smoke: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
