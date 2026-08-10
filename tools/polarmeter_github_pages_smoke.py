#!/usr/bin/env python3
"""Smoke-test a PolarMeter GitHub Pages payload.

By default this script still builds an isolated payload with
polarmeter_github_pages_prepare.py. In GitHub Actions, pass --prepared-site
after the workflow has already built ./_site so the smoke test validates the
exact payload that will be published instead of running a second live data build.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
TOOLS = WORKSPACE / 'tools'
REQUIRED_FILES = [
    'index.html',
    'privacy.html',
    'terms.html',
    'support.html',
    'styles.css',
    '.nojekyll',
    'market-snapshot-latest.json',
    'market-snapshot-manifest.json',
    'health.json',
]


def parse_utc(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or '').replace('Z', '+00:00')).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def is_expired_cpi_preview(headline_bundle: str, generated_at: object, scheduled_at: object) -> bool:
    if not re.search(r'\bcpi\b', headline_bundle, re.I):
        return False
    if not re.search(r'brace(?:s|d|ing)?\s+for|ahead\s+of|await(?:s|ed|ing)?|before|preview', headline_bundle, re.I):
        return False
    generated = parse_utc(generated_at)
    scheduled = parse_utc(scheduled_at)
    return generated is not None and scheduled is not None and generated >= scheduled


def run_prepare(out: Path) -> dict:
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
    if summary.get('freshnessAudit') != 'passed':
        raise AssertionError('pages prepare summary must prove data freshness audit passed')
    return summary


def validate_payload(out: Path) -> dict:
    for name in REQUIRED_FILES:
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
    history = snapshot.get('temperatureHistory') or {}
    if history.get('version') != 'temperature-history-v1':
        raise AssertionError('pages snapshot must expose temperatureHistory v1')
    if history.get('retentionDays') != 7:
        raise AssertionError('pages snapshot temperatureHistory must retain 7 KST dates')
    if not isinstance(history.get('items'), list) or len(history.get('items')) > 7:
        raise AssertionError('pages snapshot temperatureHistory.items must be a <=7 item list')
    if (history.get('dailyDelta') or {}).get('status') != 'ready':
        raise AssertionError('pages snapshot temperatureHistory dailyDelta status must be ready')
    data_quality = snapshot.get('dataQuality') or {}
    core_coverage = data_quality.get('coreCoverageRatio')
    if not isinstance(core_coverage, (int, float)) or core_coverage < 0.6:
        raise AssertionError(f"pages snapshot must keep renderable core coverage, got {core_coverage}")
    if data_quality.get('displayMode') == 'collecting':
        raise AssertionError('pages snapshot must not publish collecting mode')
    retired_signals = {'kodex200', 'tiger200'}
    leaked_retired = retired_signals.intersection(snapshot.get('signals') or {})
    if leaked_retired:
        raise AssertionError(f'pages snapshot leaked retired domestic ETF proxy signals: {sorted(leaked_retired)}')
    for key, signal in (snapshot.get('signals') or {}).items():
        if not isinstance(signal, dict) or signal.get('valuePolicy') != 'show':
            continue
        if not isinstance(signal.get('dataAgeHours'), (int, float)):
            raise AssertionError(f'pages snapshot showable signal missing dataAgeHours: {key}')
        if not isinstance(signal.get('freshnessRank'), int):
            raise AssertionError(f'pages snapshot showable signal missing freshnessRank: {key}')
        if signal.get('status') in {'suspect', 'stale'}:
            if not signal.get('publicReasonCode') or not signal.get('publicReasonLabel'):
                raise AssertionError(f'pages snapshot non-ok signal missing public reason: {key}')
    macro_events = snapshot.get('macroEvents') or {}
    cpi = macro_events.get('us_cpi') or {}
    cpi_last = cpi.get('lastRelease') or {}
    cpi_next = cpi.get('nextRelease') or {}
    if cpi_last.get('resultLabel') != '물가 전월 대비 -0.4% · 전년 대비 +3.5%':
        raise AssertionError('pages snapshot must expose the official June 2026 CPI result')
    if cpi_next.get('scheduledAt') != '2026-08-12T12:30:00Z':
        raise AssertionError('pages snapshot must roll CPI forward to the August 12 release')
    if cpi.get('sourcePolicy') != 'official_release_registry_with_expiry_gate':
        raise AssertionError('pages snapshot CPI must use the official release expiry gate')
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
        for title_key in ('headline', 'displayHeadline'):
            title = str(item.get(title_key) or '')
            if re.search(r'\s+By\s+[A-Za-z0-9가-힣][A-Za-z0-9가-힣 ._&/+()-]{1,63}\s*$', title):
                raise AssertionError(f'pages snapshot news {title_key} leaked a publisher suffix')
            if '물가이션' in title:
                raise AssertionError(f'pages snapshot news {title_key} leaked a partial inflation replacement')
        tone = item.get('impactTone')
        why = str(item.get('whyImportant') or '')
        if tone == 'negative' and re.search(r'(걱정|부담|압박).{0,18}(줄|낮아|완화|덜)', why):
            raise AssertionError(f'negative news must not carry a relief explanation: {item.get("displayHeadline") or item.get("headline")}')
        headline_bundle = ' '.join(str(item.get(key) or '') for key in ('headline', 'displayHeadline', 'originalHeadline'))
        if is_expired_cpi_preview(headline_bundle, snapshot.get('generatedAt'), cpi_next.get('scheduledAt')):
            raise AssertionError(f'pages snapshot must not keep an expired CPI preview: {item.get("headline")}')
    if manifest.get('okNewsCount', 0) < 10:
        raise AssertionError('manifest must expose cached news count')
    if not isinstance(manifest.get('newsTtlMinutes'), int) or not manifest.get('newsNextRefreshAt'):
        raise AssertionError('manifest must expose cached news TTL metadata')
    if not isinstance(manifest.get('marketDataTtlMinutes'), int) or not manifest.get('marketDataNextRefreshAt') or not manifest.get('nextRefreshAt'):
        raise AssertionError('manifest must expose market data TTL metadata')
    if manifest.get('newsRecommendedSchedule') != '30min_weekdays_60min_weekends_public_headline_cache':
        raise AssertionError('manifest news schedule metadata mismatch')
    if manifest.get('marketDataRecommendedSchedule') != 'market_aware_60min_daily_kr_us_open_close_confirmations':
        raise AssertionError('manifest market data schedule metadata mismatch')
    critical_refreshes = manifest.get('criticalMarketRefreshes')
    if not isinstance(critical_refreshes, list) or len(critical_refreshes) < 9:
        raise AssertionError('manifest must expose KR/US open-close critical refreshes')
    required_refresh_keys = {
        'kr_open_plus_30', 'kr_open_plus_60', 'kr_close_plus_15', 'kr_close_plus_60',
        'us_open_plus_5_dst', 'us_open_plus_30', 'us_open_plus_60', 'us_close_plus_15', 'us_close_plus_60',
    }
    actual_refresh_keys = {item.get('key') for item in critical_refreshes if isinstance(item, dict)}
    if required_refresh_keys - actual_refresh_keys:
        raise AssertionError(f'manifest missing critical refresh keys: {sorted(required_refresh_keys - actual_refresh_keys)}')
    if not isinstance(news.get('ttlMinutes'), int) or not news.get('nextRefreshAt'):
        raise AssertionError('snapshot news must expose TTL metadata')
    if not isinstance(snapshot.get('defaultTtlMinutes'), int) or not snapshot.get('nextRefreshAt'):
        raise AssertionError('snapshot must expose market data TTL metadata')
    if (snapshot.get('refreshPolicy') or {}).get('version') != 'market-aware-cache-refresh-v1':
        raise AssertionError('snapshot must expose market-aware refresh policy')
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
    if health.get('temperatureHistoryStatus') not in {'ready', 'pending'}:
        raise AssertionError('health must expose temperatureHistoryStatus')
    if health.get('temperatureHistoryStatus') != manifest.get('temperatureHistoryStatus'):
        raise AssertionError('health temperatureHistoryStatus must match manifest')
    if health.get('marketDataRecommendedSchedule') != manifest.get('marketDataRecommendedSchedule'):
        raise AssertionError('health must expose market data refresh schedule')
    if health.get('marketDataNextRefreshAt') != manifest.get('marketDataNextRefreshAt') or health.get('nextRefreshAt') != manifest.get('nextRefreshAt'):
        raise AssertionError('health must expose market data next refresh metadata')
    return {
        'snapshotStatus': manifest.get('snapshotStatus'),
        'generatedAt': snapshot.get('generatedAt'),
        'okNewsCount': manifest.get('okNewsCount'),
        'dataServingMode': manifest.get('dataServingMode'),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepared-site', type=Path, help='Validate an existing Pages payload instead of rebuilding it.')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    if args.prepared_site:
        payload = validate_payload(args.prepared_site)
        result = {
            'ok': True,
            'mode': 'prepared-site',
            'site': str(args.prepared_site),
            **payload,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"PolarMeter GitHub Pages smoke: PASS mode=prepared-site site={args.prepared_site} "
                f"snapshot={payload['snapshotStatus']} news={payload['okNewsCount']}"
            )
        return 0

    with tempfile.TemporaryDirectory(prefix='polarmeter-gh-pages-') as tmp:
        out = Path(tmp) / 'pages'
        summary = run_prepare(out)
        payload = validate_payload(out)
        result = {
            'ok': True,
            'mode': 'rebuilt-temp-site',
            'site': str(out),
            'freshnessAudit': summary.get('freshnessAudit'),
            **payload,
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"PolarMeter GitHub Pages smoke: PASS mode={result['mode']} "
            f"snapshot={result['snapshotStatus']} news={result['okNewsCount']}"
        )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
