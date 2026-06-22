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
        raw = (out / 'market-snapshot-latest.json').read_text(encoding='utf-8')
        forbidden = ['TWELVE_DATA_API_KEY', 'FMP_API_KEY', 'DATA_GO_KR_SERVICE_KEY', 'apikey=', 'serviceKey=']
        leaked = [token for token in forbidden if token in raw]
        if leaked:
            raise AssertionError(f'public snapshot leaked forbidden token(s): {leaked}')
        if snapshot.get('paidProviderEnabled') is not False or snapshot.get('clientDirectProviderCalls') is not False:
            raise AssertionError('snapshot policy fields must be false')
        data_quality = snapshot.get('dataQuality') or {}
        if data_quality.get('coreCoverageRatio') != 1.0:
            raise AssertionError(f"pages snapshot must keep full core coverage, got {data_quality.get('coreCoverageRatio')}")
        if data_quality.get('displayMode') == 'collecting':
            raise AssertionError('pages snapshot must not publish collecting mode')
        news = snapshot.get('news') or {}
        if news.get('paidProviderEnabled') is not False or news.get('clientDirectProviderCalls') is not False:
            raise AssertionError('cached news policy fields must be false')
        if news.get('bodyScrapingEnabled') is not False or news.get('imageScrapingEnabled') is not False:
            raise AssertionError('cached news must not scrape body/images')
        if len(news.get('items') or []) < 10:
            raise AssertionError('pages snapshot must include a useful cached RSS headline pool for B1 QA')
        for item in news.get('items') or []:
            if not item.get('categoryLabel') or not item.get('whyImportant') or item.get('scoreAnchor') != 'market_temperature_context':
                raise AssertionError('pages snapshot news must expose category and market-temperature evidence anchor')
            if not item.get('issueClusterKey'):
                raise AssertionError('pages snapshot news must expose issueClusterKey for app grouping')
        if manifest.get('okNewsCount', 0) < 10:
            raise AssertionError('manifest must expose cached news count')
        if not isinstance(manifest.get('newsTtlMinutes'), int) or not manifest.get('newsNextRefreshAt'):
            raise AssertionError('manifest must expose cached news TTL metadata')
        if manifest.get('newsRecommendedSchedule') != '30min_weekdays_60min_weekends_public_headline_cache':
            raise AssertionError('manifest news schedule metadata mismatch')
        if not isinstance(news.get('ttlMinutes'), int) or not news.get('nextRefreshAt'):
            raise AssertionError('snapshot news must expose TTL metadata')
        if manifest.get('dataServingMode') not in {'normal', 'limited', 'fallback'}:
            raise AssertionError('manifest must expose dataServingMode')
        if 'lastSuccessfulSnapshotAt' not in manifest:
            raise AssertionError('manifest must expose lastSuccessfulSnapshotAt')
        for key in ['providerCallCount', 'providerFailureCount']:
            if not isinstance(manifest.get(key), int):
                raise AssertionError(f'manifest must expose integer {key}')
        if not isinstance(manifest.get('providerStatusByName'), dict):
            raise AssertionError('manifest must expose providerStatusByName')
        for key in ['estimatedMonthlyCost', 'budgetLimit', 'killSwitchStatus', 'costGuardrails']:
            if not isinstance(manifest.get(key), dict):
                raise AssertionError(f'manifest must expose {key}')
        for key in ['budgetCapConfigured', 'commercialUseChecked', 'killSwitchActive']:
            if not isinstance(manifest.get(key), bool):
                raise AssertionError(f'manifest must expose boolean {key}')
        budget_limit = manifest.get('budgetLimit') or {}
        if manifest.get('budgetCapConfigured') is not True or budget_limit.get('monthlyLimit') != 50 or budget_limit.get('warningLimit') != 20:
            raise AssertionError('manifest must expose configured beta budget cap 20/50 USD')
        cost_guardrails = manifest.get('costGuardrails') or {}
        if cost_guardrails.get('commercialUseChecked') is not False:
            raise AssertionError('commercialUseChecked must stay false until provider terms are manually verified')
        snapshot_guardrails = snapshot.get('costGuardrails') or {}
        if (snapshot_guardrails.get('killSwitchStatus') or {}).get('rapidMoveBriefingGeneration') not in {'ready', 'off_by_kill_switch'}:
            raise AssertionError('snapshot must expose rapid move briefing kill-switch status')
        health = json.loads((out / 'health.json').read_text(encoding='utf-8'))
        for key in ['dataServingMode', 'providerCallCount', 'providerFailureCount', 'estimatedMonthlyCost', 'budgetLimit', 'killSwitchActive', 'killSwitchStatus']:
            if key not in health:
                raise AssertionError(f'health must expose {key}')
    print('PolarMeter GitHub Pages smoke: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
