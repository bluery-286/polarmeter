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
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen

WORKSPACE = Path(__file__).resolve().parents[1]
PROJECT = WORKSPACE
TOOLS = WORKSPACE / 'tools'
APP_TOOLS = WORKSPACE / 'tools'
DEFAULT_REPORT = PROJECT / 'testflight/free-provider-probe-report-latest.json'
DEFAULT_NEWS_REPORT = PROJECT / 'testflight/news-rss-probe-latest.json'
DEFAULT_SNAPSHOT = PROJECT / 'testflight/free-cache-snapshot-latest.json'
DEFAULT_FIXTURE = PROJECT / 'testflight/free-cache-experiment.json'
DEFAULT_LAST_KNOWN_GOOD = PROJECT / 'testflight/last-known-good-snapshot.json'
DEFAULT_PUBLIC_SNAPSHOT_NAME = 'market-snapshot-latest.json'
DEFAULT_PUBLIC_MANIFEST_NAME = 'market-snapshot-manifest.json'
DEFAULT_PUBLIC_HEALTH_NAME = 'health.json'
DEFAULT_PREVIOUS_PUBLIC_CACHE_URL = os.environ.get(
    'POLARMETER_PREVIOUS_PUBLIC_CACHE_URL',
    'https://polarmeter.polarbearworks.com/market-snapshot-latest.json',
)
TEMPERATURE_HISTORY_RETENTION_DAYS = 7
WEEKDAY_NEWS_TTL_MINUTES = 30
WEEKEND_NEWS_TTL_MINUTES = 60
NEWS_RECOMMENDED_SCHEDULE = '30min_weekdays_60min_weekends_public_headline_cache'
MARKET_DATA_RECOMMENDED_SCHEDULE = 'market_aware_30min_weekdays_60min_weekends_kr_us_open_close_confirmations'
CRITICAL_MARKET_REFRESHES = [
    {
        'key': 'kr_open_plus_30',
        'market': 'KR',
        'label': '국내장 개장 후 30분 확인',
        'localTime': '09:35 KST',
        'utcCron': '35 0 * * 1-5',
        'reason': '개장 직후 왜곡을 피하고, 첫 체결 흐름이 잡힌 뒤 국내 지수·환율을 확인합니다. GitHub schedule 누락을 줄이기 위해 혼잡한 정각/30분을 살짝 피합니다.',
    },
    {
        'key': 'kr_open_plus_60',
        'market': 'KR',
        'label': '국내장 개장 후 1시간 확인',
        'localTime': '10:05 KST',
        'utcCron': '5 1 * * 1-5',
        'reason': '초반 수급이 진정된 뒤 국내 온도와 원/달러 부담을 다시 확인합니다. GitHub schedule 누락을 줄이기 위해 혼잡한 정각/30분을 살짝 피합니다.',
    },
    {
        'key': 'kr_close_plus_15',
        'market': 'KR',
        'label': '국내장 마감 직후 확인',
        'localTime': '15:50 KST',
        'utcCron': '50 6 * * 1-5',
        'reason': '마감 직후 국내 지수 방향과 환율 부담을 장중값이 아닌 마감 근처 기준으로 갱신합니다.',
    },
    {
        'key': 'kr_close_plus_60',
        'market': 'KR',
        'label': '국내장 마감 확정 확인',
        'localTime': '16:35 KST',
        'utcCron': '35 7 * * 1-5',
        'reason': '지연 제공처의 종가 반영 시간을 감안해 국내 종가 기준 스냅샷을 다시 만듭니다.',
    },
    {
        'key': 'us_open_plus_30',
        'market': 'US',
        'label': '미장 개장 후 30분 확인',
        'localTime': '09:30 ET + 35m',
        'utcCron': '5 14,15 * * 1-5',
        'reason': '미국 정규장 초반 지수·VIX·금리 방향을 확인합니다. DST와 표준시간을 모두 커버하고, GitHub schedule 누락을 줄이기 위해 혼잡한 정각/30분을 살짝 피합니다.',
    },
    {
        'key': 'us_open_plus_60',
        'market': 'US',
        'label': '미장 개장 후 1시간 확인',
        'localTime': '09:30 ET + 65m',
        'utcCron': '35 14,15 * * 1-5',
        'reason': '초반 변동이 지나간 뒤 미국 온도와 한국장 연결 신호를 다시 계산합니다.',
    },
    {
        'key': 'us_close_plus_15',
        'market': 'US',
        'label': '미장 마감 직후 확인',
        'localTime': '16:00 ET + 20m',
        'utcCron': '20 20,21 * * 1-5',
        'reason': '미국 마감 방향과 VIX 반응을 가장 먼저 반영합니다. DST와 표준시간을 모두 커버합니다.',
    },
    {
        'key': 'us_close_plus_60',
        'market': 'US',
        'label': '미장 마감 확정 확인',
        'localTime': '16:00 ET + 65m',
        'utcCron': '5 21,22 * * 1-5',
        'reason': '지연 시세와 마감 데이터 반영을 감안해 미국 종가 기준 스냅샷을 다시 만듭니다.',
    },
]
BETA_MONTHLY_COST_LIMIT_USD = 50
BETA_MONTHLY_COST_WARNING_USD = 20

sys.path.insert(0, str(TOOLS))
from polarmeter_score_core import KST, build_scores, detect_session, snapshot_from_points  # noqa: E402

COST_GUARDRAILS = {
    'estimatedMonthlyCost': {
        'currency': 'USD',
        'smallBeta': '0-20',
        'initialLaunch1kTo10kMau': '0-50',
        'notes': 'Operational estimate only; paid market data, news APIs, AI summaries, OTA usage, and FX can change actual cost.',
    },
    'budgetLimit': {
        'currency': 'USD',
        'monthlyLimit': BETA_MONTHLY_COST_LIMIT_USD,
        'warningLimit': BETA_MONTHLY_COST_WARNING_USD,
        'status': 'configured_beta_hard_cap',
        'warningAction': 'review provider usage and OTA update frequency',
        'hardLimitAction': 'activate kill-switch before adding paid/restricted providers',
    },
    'budgetCapConfigured': True,
    'commercialUseChecked': False,
    'killSwitchActive': False,
    'killSwitchStatus': {
        'newsTranslationSummaryGeneration': 'ready_no_paid_ai',
        'rapidMoveBriefingGeneration': 'ready',
        'paidProviderCalls': 'disabled_by_default',
        'pushNotifications': 'not_enabled',
    },
    'killSwitchEnv': {
        'all': 'POLARMETER_KILL_SWITCH_ALL',
        'newsTranslationSummaryGeneration': 'POLARMETER_DISABLE_NEWS_TRANSLATION_SUMMARY',
        'rapidMoveBriefingGeneration': 'POLARMETER_DISABLE_RAPID_MOVE_BRIEFING',
        'paidProviderCalls': 'POLARMETER_DISABLE_PAID_PROVIDERS',
        'pushNotifications': 'POLARMETER_DISABLE_PUSH_NOTIFICATIONS',
    },
}


def run(cmd: list[str], *, stdout_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    if stdout_path:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open('w', encoding='utf-8') as out:
            return subprocess.run(cmd, cwd=WORKSPACE, text=True, stdout=out, stderr=subprocess.PIPE, check=True)
    return subprocess.run(cmd, cwd=WORKSPACE, text=True, capture_output=True, check=True)


def run_freshness_audit(snapshot_path: Path, report_path: Path) -> None:
    """Fail the worker before fixture/public publishing when selected data is stale."""
    run([
        sys.executable,
        str(APP_TOOLS / 'polarmeter_data_freshness_audit.py'),
        str(snapshot_path),
        str(report_path),
    ])


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


def recommended_market_ttl_minutes(now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    return 60 if current.weekday() >= 5 else 30


def iso_add_minutes(value: Any, minutes: int) -> str | None:
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed.astimezone(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace('+00:00', 'Z')


def public_refresh_policy() -> dict[str, Any]:
    return {
        'version': 'market-aware-cache-refresh-v1',
        'marketDataRecommendedSchedule': MARKET_DATA_RECOMMENDED_SCHEDULE,
        'newsRecommendedSchedule': NEWS_RECOMMENDED_SCHEDULE,
        'baseCadence': {
            'weekdays': '30분',
            'weekends': '60분',
        },
        'criticalMarketRefreshes': CRITICAL_MARKET_REFRESHES,
        'notes': [
            '정기 30분 주기에 더해 국내장/미장 개장 후와 마감 후 확인 스냅샷을 중요 갱신점으로 둡니다.',
            '무료/지연 제공처가 아직 새 값을 주지 않으면 추정으로 채우지 않고 dataAsOf와 freshness 상태를 표시합니다.',
        ],
    }


def parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def kst_date_key(value: Any) -> str | None:
    parsed = parse_utc_datetime(value)
    if not parsed:
        return None
    return parsed.astimezone(KST).date().isoformat()


def snapshot_score_point(signal: dict[str, Any] | None) -> dict[str, Any]:
    signal = signal or {}
    status = signal.get('status') or 'unavailable'
    scoreable = status in {'ok', 'stale', 'suspect'}
    return {
        'status': status,
        'close': signal.get('value') if scoreable else None,
        'pct': signal.get('changePct') if scoreable else None,
        'reason': signal.get('sourceId'),
        'date': signal.get('dataAsOf') or signal.get('fetchedAt'),
        'freshness': signal.get('freshnessStatus') or '',
    }


def temperature_scores_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    signals = snapshot.get('signals') or {}
    if not isinstance(signals, dict) or not signals:
        return None
    items = {
        'S&P500': snapshot_score_point(signals.get('sp500')),
        'Nasdaq100': snapshot_score_point(signals.get('nasdaq100')),
        'Russell 2000': snapshot_score_point(signals.get('iwm')),
        'VIX': snapshot_score_point(signals.get('vix')),
        '미국 10년물': snapshot_score_point(signals.get('us10y')),
        'DXY': snapshot_score_point(signals.get('dxy')),
        'USD/KRW': snapshot_score_point(signals.get('usd_krw')),
        'KOSPI': snapshot_score_point(signals.get('kospi')),
        'KOSDAQ': snapshot_score_point(signals.get('kosdaq')),
        '삼성전자': snapshot_score_point(signals.get('kr_samsung')),
        'SK하이닉스': snapshot_score_point(signals.get('kr_hynix')),
        'SOXX': snapshot_score_point(signals.get('soxx')),
        'SMH': snapshot_score_point(signals.get('smh')),
    }
    generated_at = parse_utc_datetime(snapshot.get('generatedAt'))
    session_type = detect_session(snapshot, now=generated_at.astimezone(KST) if generated_at else None)
    return build_scores(snapshot_from_points(items, session_type=session_type), session_type=session_type)


def temperature_history_entry(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    generated_at = snapshot.get('generatedAt')
    date_kst = kst_date_key(generated_at)
    scores = temperature_scores_from_snapshot(snapshot)
    if not date_kst or not scores:
        return None
    us = scores.get('us_temperature') or {}
    kr = scores.get('kr_temperature') or {}
    us_score = us.get('score')
    kr_score = kr.get('score')
    if not isinstance(us_score, (int, float)) or not isinstance(kr_score, (int, float)):
        return None
    return {
        'dateKst': date_kst,
        'asOf': generated_at,
        'sessionType': (scores.get('market_context') or {}).get('market_session_type'),
        'usScore': int(round(us_score)),
        'krScore': int(round(kr_score)),
        'usLabel': us.get('label'),
        'krLabel': kr.get('label'),
        'source': 'score_core_v1',
    }


def load_previous_public_snapshot(path: Path | None, public_dir: Path | None, snapshot_name: str, previous_url: str | None) -> dict[str, Any]:
    candidates: list[Path] = []
    if path:
        candidates.append(path)
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding='utf-8'))
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get('generatedAt'):
            return payload
    if previous_url:
        try:
            with urlopen(previous_url, timeout=4) as response:
                if getattr(response, 'status', 200) >= 400:
                    raise RuntimeError(f'previous public snapshot HTTP {getattr(response, "status", 0)}')
                payload = json.loads(response.read().decode('utf-8'))
                return payload if isinstance(payload, dict) else {}
        except Exception:
            pass
    if public_dir:
        candidate = public_dir / snapshot_name
        if candidate.exists():
            try:
                payload = json.loads(candidate.read_text(encoding='utf-8'))
            except Exception:
                return {}
            if isinstance(payload, dict) and payload.get('generatedAt'):
                return payload
    return {}


def apply_temperature_history(snapshot: dict[str, Any], previous_public_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    current = temperature_history_entry(snapshot)
    if not current:
        snapshot['temperatureHistory'] = {
            'version': 'temperature-history-v1',
            'basis': 'kst_calendar_day_latest_successful_snapshot',
            'anchorTimeKst': '00:00',
            'retentionDays': TEMPERATURE_HISTORY_RETENTION_DAYS,
            'updatedAt': snapshot.get('generatedAt'),
            'items': [],
            'dailyDelta': {'status': 'pending', 'reason': 'current_temperature_score_unavailable'},
        }
        return snapshot

    previous_public_snapshot = previous_public_snapshot or {}
    previous_history = previous_public_snapshot.get('temperatureHistory') or {}
    existing_items = previous_history.get('items') if isinstance(previous_history, dict) else []
    if not isinstance(existing_items, list):
        existing_items = []
    items: list[dict[str, Any]] = [
        item for item in existing_items
        if isinstance(item, dict) and isinstance(item.get('dateKst'), str)
    ]
    previous_entry = temperature_history_entry(previous_public_snapshot)
    if previous_entry:
        items = [item for item in items if item.get('dateKst') != previous_entry['dateKst']]
        items.append(previous_entry)

    items = [item for item in items if item.get('dateKst') != current['dateKst']]
    items.append(current)
    items = sorted(items, key=lambda item: str(item.get('dateKst')))[-TEMPERATURE_HISTORY_RETENTION_DAYS:]
    try:
        previous_date_kst = (datetime.fromisoformat(current['dateKst']).date() - timedelta(days=1)).isoformat()
    except ValueError:
        previous_date_kst = None
    comparison = next((item for item in items if item.get('dateKst') == previous_date_kst), None)

    def delta_for(market: str) -> dict[str, Any] | None:
        if not comparison:
            return None
        current_key = f'{market}Score'
        current_score = current.get(current_key)
        previous_score = comparison.get(current_key)
        if not isinstance(current_score, (int, float)) or not isinstance(previous_score, (int, float)):
            return None
        return {
            'currentScore': int(round(current_score)),
            'previousScore': int(round(previous_score)),
            'delta': int(round(current_score - previous_score)),
            'basis': 'previous_kst_date',
            'comparisonDateKst': comparison.get('dateKst'),
            'comparisonAsOf': comparison.get('asOf'),
        }

    us_delta = delta_for('us')
    kr_delta = delta_for('kr')
    snapshot['temperatureHistory'] = {
        'version': 'temperature-history-v1',
        'basis': 'kst_calendar_day_latest_successful_snapshot',
        'anchorTimeKst': '00:00',
        'retentionDays': TEMPERATURE_HISTORY_RETENTION_DAYS,
        'updatedAt': snapshot.get('generatedAt'),
        'currentDateKst': current['dateKst'],
        'comparisonDateKst': comparison.get('dateKst') if comparison else None,
        'items': items,
        'dailyDelta': {
            'status': 'ready' if us_delta and kr_delta else 'pending',
            'reason': None if us_delta and kr_delta else 'previous_kst_date_snapshot_missing',
            'us': us_delta,
            'kr': kr_delta,
        },
    }
    return snapshot


def assert_temperature_history_contract(snapshot: dict[str, Any]) -> None:
    history = snapshot.get('temperatureHistory') or {}
    if history.get('version') != 'temperature-history-v1':
        raise AssertionError('snapshot must include temperatureHistory v1')
    if history.get('retentionDays') != TEMPERATURE_HISTORY_RETENTION_DAYS:
        raise AssertionError('temperatureHistory retention must be 7 KST dates')
    if len(history.get('items') or []) > TEMPERATURE_HISTORY_RETENTION_DAYS:
        raise AssertionError('temperatureHistory must retain at most 7 KST dates')
    status = (history.get('dailyDelta') or {}).get('status')
    if status not in {'ready', 'pending'}:
        raise AssertionError(f'temperatureHistory dailyDelta status invalid: {status}')


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

    required = {'sp500', 'nasdaq100', 'vix', 'usd_krw', 'wti', 'soxx', 'smh', 'kospi', 'kosdaq'}
    missing = required - set(snapshot.get('signals', {}).keys())
    if missing:
        raise AssertionError(f'missing required snapshot signals: {sorted(missing)}')
    assert_temperature_history_contract(snapshot)

    for screen_key, screen in fixture.items():
        if not isinstance(screen, dict):
            continue
        if screen_key == 'archive':
            if screen.get('source') != 'runtime':
                raise AssertionError('archive source must be runtime for local-only records')
            if screen.get('badge') != '복기 준비 중':
                raise AssertionError('archive badge must keep local-record collecting state')
        else:
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


def env_enabled(name: str) -> bool:
    value = os.environ.get(name, '').strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


def public_cost_guardrails() -> dict[str, Any]:
    guardrails = json.loads(json.dumps(COST_GUARDRAILS))
    env = guardrails['killSwitchEnv']
    all_off = env_enabled(env['all'])
    switch_map = {
        'newsTranslationSummaryGeneration': env['newsTranslationSummaryGeneration'],
        'rapidMoveBriefingGeneration': env['rapidMoveBriefingGeneration'],
        'paidProviderCalls': env['paidProviderCalls'],
        'pushNotifications': env['pushNotifications'],
    }
    active_switches = []
    for key, env_name in switch_map.items():
        if all_off or env_enabled(env_name):
            guardrails['killSwitchStatus'][key] = 'off_by_kill_switch'
            active_switches.append(key)
    guardrails['killSwitchActive'] = bool(active_switches)
    guardrails['activeKillSwitches'] = active_switches
    return guardrails


def budget_cap_configured() -> bool:
    return bool(public_cost_guardrails().get('budgetCapConfigured'))


def commercial_use_checked() -> bool:
    return bool(public_cost_guardrails().get('commercialUseChecked'))


def data_serving_mode(snapshot: dict[str, Any]) -> str:
    if snapshot.get('status') not in {'ok', 'partial'}:
        return 'fallback'
    display_mode = (snapshot.get('dataQuality') or {}).get('displayMode')
    if display_mode == 'normal':
        return 'normal'
    if display_mode in {'limited', 'collecting'}:
        return 'limited'
    return 'limited'


def public_manifest(snapshot: dict[str, Any], snapshot_name: str) -> dict[str, Any]:
    signals = snapshot.get('signals', {})
    ok_signals = sorted(k for k, v in signals.items() if isinstance(v, dict) and v.get('status') == 'ok')
    blocked_signals = sorted(k for k, v in signals.items() if isinstance(v, dict) and v.get('status') not in {'ok', None})
    news = snapshot.get('news') or {}
    provider_metrics = snapshot.get('providerMetrics') or {}
    cost_guardrails = public_cost_guardrails()
    data_quality = sanitize_public_data_quality(snapshot.get('dataQuality') or {})
    refresh_policy = public_refresh_policy()
    market_ttl_minutes = snapshot.get('defaultTtlMinutes') or recommended_market_ttl_minutes()
    market_next_refresh_at = iso_add_minutes(snapshot.get('generatedAt'), int(market_ttl_minutes))
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
        'marketDataTtlMinutes': market_ttl_minutes,
        'marketDataNextRefreshAt': market_next_refresh_at,
        'nextRefreshAt': market_next_refresh_at,
        'marketDataRecommendedSchedule': MARKET_DATA_RECOMMENDED_SCHEDULE,
        'criticalMarketRefreshes': CRITICAL_MARKET_REFRESHES,
        'refreshPolicy': refresh_policy,
        'dataQuality': data_quality,
        'dataServingMode': data_serving_mode(snapshot),
        'temperatureHistoryStatus': ((snapshot.get('temperatureHistory') or {}).get('dailyDelta') or {}).get('status') or 'pending',
        'temperatureHistoryCurrentDateKst': (snapshot.get('temperatureHistory') or {}).get('currentDateKst'),
        'temperatureHistoryComparisonDateKst': (snapshot.get('temperatureHistory') or {}).get('comparisonDateKst'),
        'lastSuccessfulSnapshotAt': snapshot.get('generatedAt') if snapshot.get('status') in {'ok', 'partial'} else None,
        'providerCallCount': provider_metrics.get('providerCallCount', 0),
        'providerFailureCount': provider_metrics.get('providerFailureCount', 0),
        'providerStatusByName': sanitize_public_provider_status_by_name(provider_metrics.get('providerStatusByName') or {}),
        'estimatedMonthlyCost': cost_guardrails['estimatedMonthlyCost'],
        'budgetLimit': cost_guardrails['budgetLimit'],
        'budgetCapConfigured': cost_guardrails['budgetCapConfigured'],
        'commercialUseChecked': cost_guardrails['commercialUseChecked'],
        'killSwitchActive': cost_guardrails['killSwitchActive'],
        'killSwitchStatus': cost_guardrails['killSwitchStatus'],
        'costGuardrails': cost_guardrails,
        'blockedOrUnavailableSignals': blocked_signals,
        'notes': [
            'Public artifact contains normalized delayed/free data only.',
            'Provider API keys and raw provider URLs must never be published.',
        ],
    }


def public_health(manifest: dict[str, Any]) -> dict[str, Any]:
    quality = manifest.get('dataQuality') or {}
    core_coverage = quality.get('coreCoverageRatio')
    has_servable_core_coverage = isinstance(core_coverage, (int, float)) and core_coverage >= 0.6
    has_renderable_mode = quality.get('displayMode') != 'collecting'
    has_news = (manifest.get('okNewsCount') or 0) > 0
    return {
        'ok': manifest.get('snapshotStatus') in {'ok', 'partial'} and has_servable_core_coverage and has_renderable_mode and has_news,
        'generatedAt': manifest.get('generatedAt'),
        'snapshotStatus': manifest.get('snapshotStatus'),
        'okSignalCount': len(manifest.get('okSignals') or []),
        'okNewsCount': manifest.get('okNewsCount') or 0,
        'newsStatus': manifest.get('newsStatus') or 'unavailable',
        'dataQuality': manifest.get('dataQuality') or {},
        'dataServingMode': manifest.get('dataServingMode') or 'limited',
        'temperatureHistoryStatus': manifest.get('temperatureHistoryStatus') or 'pending',
        'temperatureHistoryCurrentDateKst': manifest.get('temperatureHistoryCurrentDateKst'),
        'temperatureHistoryComparisonDateKst': manifest.get('temperatureHistoryComparisonDateKst'),
        'lastSuccessfulSnapshotAt': manifest.get('lastSuccessfulSnapshotAt'),
        'marketDataTtlMinutes': manifest.get('marketDataTtlMinutes'),
        'marketDataNextRefreshAt': manifest.get('marketDataNextRefreshAt'),
        'nextRefreshAt': manifest.get('nextRefreshAt'),
        'providerCallCount': manifest.get('providerCallCount') or 0,
        'providerFailureCount': manifest.get('providerFailureCount') or 0,
        'providerStatusByName': sanitize_public_provider_status_by_name(manifest.get('providerStatusByName') or {}),
        'estimatedMonthlyCost': manifest.get('estimatedMonthlyCost') or {},
        'budgetLimit': manifest.get('budgetLimit') or {},
        'budgetCapConfigured': manifest.get('budgetCapConfigured') is True,
        'commercialUseChecked': manifest.get('commercialUseChecked') is True,
        'killSwitchActive': manifest.get('killSwitchActive') is True,
        'killSwitchStatus': manifest.get('killSwitchStatus') or {},
        'marketDataRecommendedSchedule': manifest.get('marketDataRecommendedSchedule'),
        'criticalMarketRefreshes': manifest.get('criticalMarketRefreshes') or [],
        'refreshPolicy': manifest.get('refreshPolicy') or {},
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
        'licenseNote', 'coreSignal', 'qualityStatus', 'dataAgeHours', 'freshnessRank',
        'reliability', 'lastSuccessfulAt',
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
        'critical', 'criticalReason', 'marketImpactScore', 'issueClusterKey',
        'sourceId', 'region', 'provider', 'licenseNote',
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


def sanitize_public_provider_status_by_name(status_by_name: dict[str, Any]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in status_by_name.items():
        status = str(value or 'unavailable')
        if status in {'missing_key', 'missing_secret', 'needs_key'}:
            status = 'unavailable'
        elif status not in {'ok', 'partial', 'unavailable'}:
            status = 'unavailable'
        safe[str(key)] = status
    return safe


def sanitize_public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    market_ttl_minutes = snapshot.get('defaultTtlMinutes') or recommended_market_ttl_minutes()
    return {
        'mode': snapshot.get('mode') or 'free_cache_experiment',
        'generatedAt': snapshot.get('generatedAt'),
        'status': snapshot.get('status'),
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'costGuardrails': public_cost_guardrails(),
        'refreshPolicy': public_refresh_policy(),
        'defaultTtlMinutes': market_ttl_minutes,
        'nextRefreshAt': iso_add_minutes(snapshot.get('generatedAt'), int(market_ttl_minutes)),
        'dataQuality': sanitize_public_data_quality(snapshot.get('dataQuality') or {}),
        'temperatureHistory': snapshot.get('temperatureHistory') or {
            'version': 'temperature-history-v1',
            'basis': 'kst_calendar_day_latest_successful_snapshot',
            'anchorTimeKst': '00:00',
            'retentionDays': TEMPERATURE_HISTORY_RETENTION_DAYS,
            'updatedAt': snapshot.get('generatedAt'),
            'items': [],
            'dailyDelta': {'status': 'pending', 'reason': 'temperature_history_unavailable'},
        },
        'signals': {
            key: sanitize_public_signal(value)
            for key, value in (snapshot.get('signals') or {}).items()
            if isinstance(value, dict)
        },
        'news': sanitize_public_news(snapshot.get('news') or {}),
    }


def assert_public_payload_safe(*payloads: dict[str, Any]) -> None:
    raw = '\n'.join(json.dumps(payload, ensure_ascii=False).lower() for payload in payloads)
    forbidden_tokens = [
        'api_key', 'apikey=', 'servicekey=', 'service_key', 'secret',
        'twelve_data_api_key', 'fmp_api_key', 'data_go_kr_service_key',
        'bok_api_key', 'ecos_api_key', 'missing_key', 'feedresults',
        'fallbackchain', 'qualityreason', 'marketimpactcomponents',
    ]
    leaked = [token for token in forbidden_tokens if token in raw]
    if leaked:
        raise AssertionError(f'public snapshot leaked internal token(s): {leaked}')
    for payload in payloads:
        news = payload.get('news') or {}
        for item in news.get('items') or []:
            if item.get('body') or item.get('imageUrl') or item.get('description'):
                raise AssertionError('public news item must not expose body/image/description fields')


def publish_public_artifacts(public_dir: Path, snapshot: dict[str, Any], snapshot_name: str) -> dict[str, str]:
    snapshot_path = public_dir / snapshot_name
    manifest_path = public_dir / DEFAULT_PUBLIC_MANIFEST_NAME
    health_path = public_dir / DEFAULT_PUBLIC_HEALTH_NAME
    public_snapshot = sanitize_public_snapshot(snapshot)
    manifest = public_manifest(snapshot, snapshot_name)
    health = public_health(manifest)
    assert_temperature_history_contract(public_snapshot)
    assert_public_payload_safe(public_snapshot, manifest, health)
    atomic_write_json(snapshot_path, public_snapshot)
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(health_path, health)
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
    parser.add_argument('--previous-public-snapshot', type=Path, help='Optional previous public snapshot JSON for temperature history continuity')
    parser.add_argument('--previous-public-url', default=DEFAULT_PREVIOUS_PUBLIC_CACHE_URL, help='Optional previous deployed public snapshot URL for temperature history continuity')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    run([sys.executable, str(TOOLS / 'polarmeter_free_provider_probe.py'), '--json'], stdout_path=args.report)
    news_ttl_minutes = recommended_news_ttl_minutes()
    run([sys.executable, str(TOOLS / 'polarmeter_news_rss_probe.py'), '--output', str(args.news_report), '--ttl-minutes', str(news_ttl_minutes)])
    run([sys.executable, str(TOOLS / 'polarmeter_cache_snapshot.py'), '--probe', str(args.report), '--news-probe', str(args.news_report), '--output', str(args.snapshot), '--last-known-good', str(args.last_known_good)])
    previous_public_snapshot = load_previous_public_snapshot(args.previous_public_snapshot, args.public_dir, args.public_snapshot_name, args.previous_public_url)
    snapshot_for_history = load_json(args.snapshot)
    apply_temperature_history(snapshot_for_history, previous_public_snapshot)
    atomic_write_json(args.snapshot, snapshot_for_history)
    run_freshness_audit(args.snapshot, args.report)
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
        'temperatureHistoryStatus': ((snapshot.get('temperatureHistory') or {}).get('dailyDelta') or {}).get('status') or 'pending',
        'temperatureHistoryCurrentDateKst': (snapshot.get('temperatureHistory') or {}).get('currentDateKst'),
        'temperatureHistoryComparisonDateKst': (snapshot.get('temperatureHistory') or {}).get('comparisonDateKst'),
        'okNewsCount': len((snapshot.get('news') or {}).get('items') or []),
        'freshnessAudit': 'passed',
        'publicArtifacts': public_artifacts,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"PolarMeter free-cache worker: PASS report={summary['reportStatus']} snapshot={summary['snapshotStatus']} okSignals={len(summary['okSignals'])}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
