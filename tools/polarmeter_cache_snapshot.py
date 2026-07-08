#!/usr/bin/env python3
"""Build a provider-neutral free-cache snapshot for PolarMeter.

The snapshot is the cache-server contract. It can be built from a provider probe
report or, while keys are unavailable, as a safe placeholder that makes missing
coverage explicit without calling paid providers.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from polarmeter_market_calendar import is_kr_market_active, is_us_market_active

WORKSPACE = Path(__file__).resolve().parents[1]
PROJECT = WORKSPACE
DEFAULT_PROBE = PROJECT / 'testflight/free-provider-probe-report-latest.json'
DEFAULT_OUTPUT = PROJECT / 'testflight/free-cache-snapshot-latest.json'
DEFAULT_NEWS_PROBE = PROJECT / 'testflight/news-rss-probe-latest.json'
DEFAULT_LAST_KNOWN_GOOD = PROJECT / 'testflight/last-known-good-snapshot.json'
NEWS_RECOMMENDED_SCHEDULE = '30min_weekdays_60min_weekends_public_headline_cache'
NEWS_SNAPSHOT_MAX_ITEMS = 30
MAX_STALE_SIGNAL_AGE_HOURS = {
    # Display-facing market values may be delayed, but multi-day-old numbers must
    # not be recycled as current.
    'sp500': 72,
    'nasdaq100': 72,
    'iwm': 72,
    'soxx': 72,
    'smh': 72,
    'eem': 72,
    'vix': 72,
    'usd_krw': 72,
    'wti': 72,
    'kospi': 96,
    'kosdaq': 96,
    'us10y': 96,
    'dxy': 96,
    'gold': 96,
}
KST = timezone(timedelta(hours=9))
KR_INTRADAY_STALE_KEYS = {'kospi', 'kosdaq', 'kr_samsung'}
ACTIVE_MARKET_MAX_AGE_HOURS = 3.0
KR_ACTIVE_MARKET_STALE_KEYS = {'kospi', 'kosdaq', 'usd_krw'}
US_ACTIVE_MARKET_STALE_KEYS = {'sp500', 'nasdaq100', 'iwm', 'soxx', 'smh', 'eem', 'vix'}

CORE_SIGNALS = {'kospi', 'usd_krw', 'sp500', 'vix', 'wti'}
CORE_GROUPS = {
    'kr_index': ['kospi', 'kosdaq'],
    'fx': ['usd_krw'],
    'us_index': ['sp500', 'nasdaq100'],
    'volatility': ['vix'],
    'commodity': ['wti', 'gold'],
    'rate': ['us10y'],
    'dollar': ['dxy'],
}

SANITY_RANGES = {
    # abs(changePct) <= suspect is normal; > reject is hidden unless last-known-good exists.
    # 2026 POC cross-check: KOSPI genuinely trades in the 7,000~8,000 range.
    'kospi': {'minPrice': 1000, 'maxPrice': 12000, 'suspectAbsChangePct': 9.0, 'rejectAbsChangePct': 18.0, 'requiresChangePct': True},
    'kosdaq': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 16.0, 'requiresChangePct': True},
    'sp500': {'suspectAbsChangePct': 5.0, 'rejectAbsChangePct': 7.0, 'requiresChangePct': True},
    'nasdaq100': {'suspectAbsChangePct': 5.0, 'rejectAbsChangePct': 7.0, 'requiresChangePct': True},
    'usd_krw': {'suspectAbsChangePct': 2.0, 'rejectAbsChangePct': 3.0, 'requiresChangePct': False},
    'us10y': {'minPrice': 1.0, 'maxPrice': 8.0, 'suspectAbsChangePct': 8.0, 'rejectAbsChangePct': 15.0, 'requiresChangePct': False},
    'dxy': {'minPrice': 70.0, 'maxPrice': 140.0, 'suspectAbsChangePct': 3.0, 'rejectAbsChangePct': 5.0, 'requiresChangePct': False},
    'vix_aux': {'suspectAbsChangePct': 12.0, 'rejectAbsChangePct': 20.0, 'requiresChangePct': False},
    'wti': {'suspectAbsChangePct': 8.0, 'rejectAbsChangePct': 12.0, 'requiresChangePct': False},
    'gold': {'suspectAbsChangePct': 3.0, 'rejectAbsChangePct': 5.0, 'requiresChangePct': False},
    'soxx': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 16.0, 'requiresChangePct': False},
    'smh': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 16.0, 'requiresChangePct': False},
    'iwm': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 9.0, 'requiresChangePct': False},
    'eem': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 9.0, 'requiresChangePct': False},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def normalized_as_of(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
    if value:
        return str(value)
    return utc_now()


def parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, str) and len(value.strip()) == 8 and value.strip().isdigit():
            parsed = datetime.strptime(value.strip(), '%Y%m%d')
            return parsed.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def candidate_data_date_kst(value: Any) -> Any:
    raw = str(value or '').strip()
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, '%Y%m%d').date()
        except ValueError:
            return None
    parsed = parse_utc_datetime(value)
    if not parsed:
        return None
    return parsed.astimezone(KST).date()


def is_kr_intraday_guard_active(now: datetime | None = None) -> bool:
    current = (now or datetime.now(timezone.utc)).astimezone(KST)
    if current.weekday() >= 5:
        return False
    return time(9, 30) <= current.time() <= time(16, 40)


def kr_intraday_stale_reason(key: str, provider: str, item: dict[str, Any]) -> str | None:
    if key not in KR_INTRADAY_STALE_KEYS or not is_kr_intraday_guard_active():
        return None
    data_date = candidate_data_date_kst(item.get('asOf'))
    current_date = datetime.now(timezone.utc).astimezone(KST).date()
    if data_date is not None and data_date < current_date:
        return f'kr_intraday_prior_day_data provider={provider} dataAsOf={item.get("asOf")}'
    return None


def active_market_stale_reason(key: str, provider: str, item: dict[str, Any]) -> str | None:
    now = datetime.now(timezone.utc)
    if key in KR_ACTIVE_MARKET_STALE_KEYS and not is_kr_market_active(now, time(9, 30), time(16, 40)):
        return None
    if key in US_ACTIVE_MARKET_STALE_KEYS and not is_us_market_active(now, time(9, 30), time(17, 30)):
        return None
    if key not in KR_ACTIVE_MARKET_STALE_KEYS and key not in US_ACTIVE_MARKET_STALE_KEYS:
        return None
    age_hours = candidate_age_hours(item)
    if age_hours is None or age_hours > ACTIVE_MARKET_MAX_AGE_HOURS:
        return f'active_market_stale provider={provider} dataAsOf={item.get("asOf")} ageHours={age_hours}'
    return None


def hard_stale_reason(key: str, provider: str, item: dict[str, Any]) -> str | None:
    max_age = MAX_STALE_SIGNAL_AGE_HOURS.get(key)
    if max_age is None:
        return None
    age_hours = candidate_age_hours(item)
    if age_hours is None or age_hours > max_age:
        return f'hard_stale provider={provider} dataAsOf={item.get("asOf")} ageHours={age_hours} maxAgeHours={max_age}'
    return None


def stale_signal_age_hours(signal: dict[str, Any]) -> float | None:
    data_as_of = parse_utc_datetime(signal.get('dataAsOf') or signal.get('lastSuccessfulAt') or signal.get('fetchedAt'))
    if not data_as_of:
        return None
    return max(0.0, (datetime.now(timezone.utc) - data_as_of).total_seconds() / 3600)


def candidate_age_hours(item: dict[str, Any]) -> float | None:
    data_as_of = parse_utc_datetime(normalized_as_of(item.get('asOf')))
    if not data_as_of:
        return None
    return max(0.0, (datetime.now(timezone.utc) - data_as_of).total_seconds() / 3600)


def candidate_freshness_rank(item: dict[str, Any]) -> int:
    age_hours = candidate_age_hours(item)
    if age_hours is None:
        return 0
    if age_hours <= 18:
        return 3
    if age_hours <= 84:
        return 2
    if age_hours <= 168:
        return 1
    return 0


def stale_signal_is_too_old(key: str, signal: dict[str, Any]) -> bool:
    max_hours = MAX_STALE_SIGNAL_AGE_HOURS.get(key)
    if max_hours is None:
        return False
    age_hours = stale_signal_age_hours(signal)
    return age_hours is None or age_hours > max_hours


def kr_index_display_badge(value: Any) -> str:
    """Return a user-facing Korean index timing badge from a data timestamp.

    KRX regular trading ends at 15:30 KST. A delayed public-chart timestamp after
    the close should not be labelled as intraday; that creates a trust mismatch
    when the exact data timestamp is shown next to the badge.
    """
    raw = str(value or '').strip()
    if len(raw) == 8 and raw.isdigit():
        return '종가 기준'
    dt = None
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00')) if raw else None
    except ValueError:
        dt = None
    if not dt:
        return '지연 시세'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    kst = dt.astimezone(timezone.utc).timestamp() + 9 * 3600
    local = datetime.fromtimestamp(kst, timezone.utc)
    minutes = local.hour * 60 + local.minute
    if minutes < 9 * 60:
        return '전일 종가'
    if minutes < 15 * 60 + 30:
        return '장중 기준'
    return '종가 기준'


def load_probe(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def load_news_probe(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def load_last_known_good(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return payload.get('signals') if isinstance(payload.get('signals'), dict) else {}
    except Exception:
        return {}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write('\n')
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def provider_items(probe: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {provider.get('provider', 'unknown'): provider.get('items', []) for provider in probe.get('providers', [])}


def provider_metrics(probe: dict[str, Any]) -> dict[str, Any]:
    providers = probe.get('providers') if isinstance(probe.get('providers'), list) else []
    status_by_name: dict[str, str] = {}
    call_count = 0
    item_failure_count = 0

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get('provider') or 'unknown')
        status = str(provider.get('status') or 'unknown')
        status_by_name[name] = status
        items = provider.get('items') if isinstance(provider.get('items'), list) else []
        call_count += len(items)
        item_failure_count += sum(1 for item in items if isinstance(item, dict) and item.get('status') != 'ok')

    provider_failure_count = sum(1 for status in status_by_name.values() if status not in {'ok', 'partial'})
    return {
        'providerCallCount': call_count,
        'providerFailureCount': provider_failure_count,
        'providerItemFailureCount': item_failure_count,
        'providerStatusByName': status_by_name,
    }


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_candidate(key: str, item: dict[str, Any]) -> tuple[str, str | None]:
    if item.get('status') != 'ok' or item.get('price') in (None, ''):
        return 'unavailable', item.get('reason') or 'no_price'
    rule = SANITY_RANGES.get(key, {})
    price = as_float(item.get('price'))
    if price is not None:
        min_price = rule.get('minPrice')
        max_price = rule.get('maxPrice')
        if min_price is not None and price < float(min_price):
            return 'invalid', f'price_below_range<{min_price}'
        if max_price is not None and price > float(max_price):
            return 'invalid', f'price_above_range>{max_price}'
    change_pct = as_float(item.get('changePct'))
    if rule.get('requiresChangePct') and change_pct is None:
        return 'partial', 'missing_changePct'
    if change_pct is not None:
        abs_change = abs(change_pct)
        if abs_change > float(rule.get('rejectAbsChangePct', 999)):
            return 'invalid', f'changePct_out_of_range>{rule.get("rejectAbsChangePct")}'
        if abs_change > float(rule.get('suspectAbsChangePct', 999)):
            return 'suspect', f'changePct_suspect>{rule.get("suspectAbsChangePct")}'
    return 'ok', None


def signal_reliability(key: str, provider: str, status: str, reason: str | None = None, data_as_of: Any = None) -> dict[str, Any]:
    if status == 'stale':
        return {
            'sourceClass': 'stale_last_known_good',
            'displayBadge': '지연된 값 · 참고만',
            'confidencePolicy': 'low_stale_last_known_good',
        }
    if key == 'usd_krw':
        if status == 'suspect':
            return {
                'sourceClass': 'market_rate_large_move',
                'displayBadge': '변동 큼 · 참고용 환율',
                'confidencePolicy': 'low_large_move_until_official_fx',
            }
        official = provider == 'bok-ecos-free'
        return {
            'sourceClass': 'official_reference' if official else 'market_rate',
            'displayBadge': '한국은행 기준환율' if official else '참고용 환율',
            'confidencePolicy': 'normal' if official else 'low_until_official_fx',
        }
    if key == 'vix':
        if status == 'stale' or reason and '429' in reason:
            return {
                'sourceClass': 'stale_last_known_good',
                'displayBadge': '지연된 값 · 참고만',
                'confidencePolicy': 'low_on_provider_rate_limit',
            }
        return {'sourceClass': 'volatility_index', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}
    if key == 'gold' and status == 'suspect':
        return {
            'sourceClass': 'supplementary_safe_haven',
            'displayBadge': '이상 변동 주의',
            'confidencePolicy': 'supplementary_only',
        }
    if key == 'us10y':
        return {'sourceClass': 'public_rate_index', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}
    if key == 'dxy':
        return {'sourceClass': 'public_dollar_index', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}
    if key in {'iwm', 'eem'}:
        return {'sourceClass': 'diversification_index_etf', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}
    if status == 'suspect' and key in {'kospi', 'kosdaq'} and provider == 'public-chart-delayed':
        return {
            'sourceClass': 'worker_side_public_chart_large_move',
            'displayBadge': '변동 큼 · 공공차트 확인',
            'confidencePolicy': 'low_until_corroborated_public_chart',
        }
    if status == 'suspect':
        return {
            'sourceClass': 'delayed_market_data_large_move',
            'displayBadge': '변동 큼 · 보조 확인',
            'confidencePolicy': 'low_until_corroborated_large_move',
        }
    if key in {'kospi', 'kosdaq'} and provider == 'public-chart-delayed':
        return {'sourceClass': 'worker_side_public_chart_intraday', 'displayBadge': kr_index_display_badge(data_as_of), 'confidencePolicy': 'normal_with_fallback_to_public_close'}
    if key in {'kospi', 'kosdaq'}:
        return {'sourceClass': 'delayed_market_data', 'displayBadge': '공공 지연', 'confidencePolicy': 'normal'}
    return {'sourceClass': 'delayed_market_data', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}


def normalize_signal(signal: dict[str, Any], provider: str, item: dict[str, Any], *, status: str = 'ok', reason: str | None = None) -> dict[str, Any]:
    key = signal['key']
    showable = status in {'ok', 'suspect'}
    data_as_of = normalized_as_of(item.get('asOf'))
    age_hours = candidate_age_hours(item)
    return {
        'key': key,
        'label': signal['label'],
        'value': item.get('price'),
        'change': item.get('change'),
        'changePct': item.get('changePct'),
        'status': 'ok' if status == 'ok' else status,
        'freshnessStatus': 'delayed' if showable else status,
        'provider': provider,
        'sourceId': f'{provider}:{item.get("symbol")}',
        'fetchedAt': utc_now(),
        'dataAsOf': data_as_of,
        'dataAgeHours': round(age_hours, 2) if age_hours is not None else None,
        'freshnessRank': candidate_freshness_rank(item),
        'ttlMinutes': 30,
        'valuePolicy': 'show' if showable else 'hide',
        'licenseNote': 'free/delayed provider via cache server',
        'coreSignal': key in CORE_SIGNALS,
        'qualityStatus': status,
        'qualityReason': reason,
        'reliability': signal_reliability(key, provider, status, reason, data_as_of),
        'lastSuccessfulAt': utc_now() if showable else None,
    }


def stale_from_last_good(signal: dict[str, Any], last_good: dict[str, Any], reason: str) -> dict[str, Any] | None:
    key = signal['key']
    previous = last_good.get(key)
    if not isinstance(previous, dict) or previous.get('value') in (None, ''):
        return None
    if stale_signal_is_too_old(key, previous):
        return None
    previous_as_of = previous.get('dataAsOf') or previous.get('lastSuccessfulAt') or previous.get('fetchedAt')
    if kr_intraday_stale_reason(key, previous.get('provider') or 'last-known-good', {'asOf': previous_as_of}):
        return None
    previous_quality, previous_reason = validate_candidate(key, {
        'status': 'ok',
        'price': previous.get('value'),
        'changePct': previous.get('changePct'),
        'reason': previous.get('qualityReason'),
    })
    if previous_quality == 'invalid':
        return None
    out = dict(previous)
    out.update({
        'status': 'stale',
        'freshnessStatus': 'stale',
        'valuePolicy': 'show',
        'coreSignal': key in CORE_SIGNALS,
        'qualityStatus': 'stale_last_known_good',
        'qualityReason': reason if previous_quality == 'ok' else previous_reason,
        'reliability': signal_reliability(key, previous.get('provider') or 'free-cache', 'stale', reason),
        'staleSource': 'last_known_good',
        'dataAgeHours': round(stale_signal_age_hours(previous), 2) if stale_signal_age_hours(previous) is not None else None,
        'staleAgeHours': stale_signal_age_hours(previous),
        'maxStaleAgeHours': MAX_STALE_SIGNAL_AGE_HOURS.get(key),
        'fetchedAt': utc_now(),
        'ttlMinutes': previous.get('ttlMinutes') or 360,
    })
    return out


def choose_signal(signal: dict[str, Any], providers: dict[str, list[dict[str, Any]]], last_good: dict[str, Any]) -> dict[str, Any]:
    key = signal['key']
    failures: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    quality_rank = {'ok': 3, 'suspect': 2, 'partial': 1}
    for provider, items in providers.items():
        for item in items:
            if item.get('key') != key:
                continue
            quality, reason = validate_candidate(key, item)
            stale_reason = (
                kr_intraday_stale_reason(key, provider, item)
                or active_market_stale_reason(key, provider, item)
                or hard_stale_reason(key, provider, item)
            )
            if stale_reason and quality in {'ok', 'suspect', 'partial'}:
                quality, reason = 'stale', stale_reason
            failure = {
                'provider': provider,
                'status': item.get('status'),
                'quality': quality,
                'reason': reason or item.get('reason'),
                'dataAsOf': normalized_as_of(item.get('asOf')) if item.get('asOf') not in (None, '') else None,
                'dataAgeHours': round(candidate_age_hours(item), 2) if candidate_age_hours(item) is not None else None,
                'freshnessRank': candidate_freshness_rank(item),
            }
            failures.append(failure)
            if quality in {'ok', 'suspect', 'partial'}:
                out = normalize_signal(signal, provider, item, status=quality, reason=reason)
                candidates.append({
                    'signal': out,
                    'quality': quality,
                    'qualityRank': quality_rank[quality],
                    'freshnessRank': candidate_freshness_rank(item),
                })

    showable_candidates = [item for item in candidates if item['quality'] in {'ok', 'suspect'}]
    if showable_candidates:
        best = max(showable_candidates, key=lambda item: (item['freshnessRank'], item['qualityRank']))
        best['signal']['fallbackChain'] = failures
        return best['signal']
    last_good_signal = stale_from_last_good(signal, last_good, failures[-1]['reason'] if failures else 'provider_unavailable')
    if last_good_signal:
        last_good_signal['fallbackChain'] = failures
        return last_good_signal
    partial_candidates = [item for item in candidates if item['quality'] == 'partial']
    if partial_candidates:
        best = max(partial_candidates, key=lambda item: (item['freshnessRank'], item['qualityRank']))
        best['signal']['fallbackChain'] = failures
        return best['signal']
    return {
        'key': key,
        'label': signal['label'],
        'value': None,
        'changePct': None,
        'status': 'unavailable' if not failures else 'invalid',
        'freshnessStatus': 'unavailable' if not failures else 'invalid',
        'provider': 'free-provider-poc',
        'sourceId': f'free-provider-poc:{key}',
        'fetchedAt': None,
        'dataAsOf': None,
        'ttlMinutes': 30,
        'valuePolicy': 'neutral_placeholder' if not failures else 'hide',
        'licenseNote': 'free provider key/coverage not verified yet',
        'coreSignal': key in CORE_SIGNALS,
        'qualityStatus': 'unavailable' if not failures else 'invalid',
        'qualityReason': failures[-1]['reason'] if failures else 'provider_unavailable',
        'fallbackChain': failures,
    }


def data_quality(signals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    usable_statuses = {'ok', 'stale', 'suspect'}
    clean_statuses = {'ok'}
    core_ok: list[str] = []
    group_status = {}
    group_details = {}
    for group, keys in CORE_GROUPS.items():
        statuses = [(key, (signals.get(key) or {}).get('status')) for key in keys]
        group_details[group] = {
            key: {
                'status': (signals.get(key) or {}).get('status'),
                'qualityStatus': (signals.get(key) or {}).get('qualityStatus'),
                'qualityReason': (signals.get(key) or {}).get('qualityReason'),
            }
            for key in keys
        }
        usable = [(key, status) for key, status in statuses if status in usable_statuses]
        clean = [(key, status) for key, status in statuses if status in clean_statuses]
        if clean and clean[0][0] == keys[0]:
            group_status[group] = 'ok'
            core_ok.append(clean[0][0])
        elif clean:
            group_status[group] = 'partial_ok'
            core_ok.append(clean[0][0])
        elif usable:
            group_status[group] = usable[0][1] or 'limited'
            core_ok.append(usable[0][0])
        else:
            group_status[group] = 'missing'
    coverage = len([status for status in group_status.values() if status != 'missing']) / max(len(CORE_GROUPS), 1)
    normal_allowed = coverage >= 1.0 and all(status in {'ok', 'partial_ok'} for status in group_status.values())
    return {
        'policy': 'core_signal_fallback_chain_v1',
        'coreSignals': sorted(CORE_SIGNALS),
        'coreGroups': CORE_GROUPS,
        'coreOkSignals': sorted(core_ok),
        'coreCoverageRatio': round(coverage, 3),
        'normalTemperatureAllowed': normal_allowed,
        'displayMode': 'normal' if normal_allowed else ('limited' if coverage >= 0.6 else 'collecting'),
        'groupStatus': group_status,
        'groupDetails': group_details,
        'displayBadge': {
            'kr_index': 'kr_composite_basis' if group_status.get('kr_index') == 'partial_ok' else None,
        },
        'rules': {
            'fallbackOrder': 'primary -> secondary -> current_suspect_with_warning -> last_known_good -> unavailable_state',
            'paidProviderEnabled': False,
            'clientDirectProviderCalls': False,
            'sanityRanges': SANITY_RANGES,
            'fxOfficialProvider': 'bok-ecos-free',
            'volatilityPrimary': ['vix'],
            'volatilitySupplementary': ['vix_aux'],
            'commodityPrimary': ['wti'],
            'commoditySupplementary': ['gold'],
            'vixRateLimitPolicy': 'show_stale_value_with_low_confidence_and_vixy_supplement',
        },
    }


def write_last_known_good(path: Path | None, snapshot: dict[str, Any]) -> None:
    if not path:
        return
    signals = snapshot.get('signals') or {}
    good_signals = {key: value for key, value in signals.items() if isinstance(value, dict) and value.get('status') in {'ok', 'suspect'} and value.get('valuePolicy') == 'show'}
    if not good_signals:
        return
    existing = {'signals': {}}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            existing = {'signals': {}}
    merged = {}
    for key, value in (existing.get('signals') or {}).items():
        if not isinstance(value, dict):
            continue
        quality, _ = validate_candidate(key, {'status': 'ok', 'price': value.get('value'), 'changePct': value.get('changePct')})
        if quality != 'invalid':
            merged[key] = value
    merged.update(good_signals)
    atomic_write_json(path, {'updatedAt': utc_now(), 'signals': merged})


def cached_news(news_probe: dict[str, Any] | None) -> dict[str, Any]:
    if not news_probe:
        return {
            'status': 'unavailable',
            'items': [],
            'sourcePolicy': 'public_rss_headline_cache_only',
            'bodyScrapingEnabled': False,
            'imageScrapingEnabled': False,
        }
    items = news_probe.get('items') if isinstance(news_probe.get('items'), list) else []
    return {
        'status': news_probe.get('status') or ('ok' if items else 'unavailable'),
        'generatedAt': news_probe.get('generatedAt'),
        'ttlMinutes': news_probe.get('ttlMinutes') or 30,
        'nextRefreshAt': news_probe.get('nextRefreshAt'),
        'recommendedSchedule': news_probe.get('recommendedSchedule') or NEWS_RECOMMENDED_SCHEDULE,
        'sourcePolicy': 'public_rss_headline_cache_only',
        'bodyScrapingEnabled': False,
        'imageScrapingEnabled': False,
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'items': [
            {
                'headline': item.get('headline'),
                'displayHeadline': item.get('displayHeadline') or item.get('headline'),
                'originalHeadline': item.get('originalHeadline'),
                'language': item.get('language'),
                'translationNote': item.get('translationNote'),
                'sourceName': item.get('sourceName'),
                'publishedAt': item.get('publishedAt'),
                'url': item.get('url'),
                'impactTarget': item.get('impactTarget') or 'market',
                'impactTone': item.get('impactTone') or 'neutral',
                'category': item.get('category') or 'market_event',
                'categoryLabel': item.get('categoryLabel') or '시장이벤트',
                'tags': item.get('tags') or ['뉴스'],
                'relatedFactors': item.get('relatedFactors') or ['news'],
                'whyImportant': item.get('whyImportant') or '시장 온도와 함께 볼 만한 헤드라인입니다.',
                'scoreAnchor': 'market_temperature_context',
                'qualityScore': item.get('qualityScore'),
                'priorityTier': item.get('priorityTier') or ('CRITICAL' if item.get('critical') else 'STANDARD'),
                'critical': item.get('critical') is True,
                'criticalReason': item.get('criticalReason'),
                'marketImpactScore': item.get('marketImpactScore'),
                'issueClusterKey': item.get('issueClusterKey'),
                'sourceId': item.get('sourceId'),
                'region': item.get('region'),
                'provider': item.get('provider') or 'public-rss',
                'licenseNote': item.get('licenseNote') or 'public RSS headline cache only; no body or image scraping',
            }
            for item in items[:NEWS_SNAPSHOT_MAX_ITEMS]
            if isinstance(item, dict) and item.get('headline') and item.get('url')
        ],
    }


def build_snapshot(probe: dict[str, Any], news_probe: dict[str, Any] | None = None, last_good: dict[str, Any] | None = None) -> dict[str, Any]:
    providers = provider_items(probe)
    signals = {
        signal['key']: choose_signal(signal, providers, last_good or {})
        for signal in probe.get('requiredSignals', [])
    }
    ok_count = sum(1 for item in signals.values() if item['status'] in {'ok', 'stale'})
    quality = data_quality(signals)
    status = 'ok' if quality['normalTemperatureAllowed'] else ('partial' if ok_count else 'needs_keys')
    return {
        'mode': 'free_cache_experiment',
        'generatedAt': utc_now(),
        'status': status,
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'defaultTtlMinutes': 60,
        'probeStatus': probe.get('status'),
        'providerMetrics': provider_metrics(probe),
        'dataQuality': quality,
        'sources': {
            provider['provider']: {
                'status': provider.get('status'),
                'message': provider.get('message'),
            }
            for provider in probe.get('providers', [])
        },
        'signals': signals,
        'news': cached_news(news_probe),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', type=Path, default=DEFAULT_PROBE)
    parser.add_argument('--news-probe', type=Path, default=DEFAULT_NEWS_PROBE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--last-known-good', type=Path, default=DEFAULT_LAST_KNOWN_GOOD)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    snapshot = build_snapshot(load_probe(args.probe), load_news_probe(args.news_probe), load_last_known_good(args.last_known_good))
    atomic_write_json(args.output, snapshot)
    write_last_known_good(args.last_known_good, snapshot)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(f"PolarMeter free cache snapshot: wrote {args.output} status={snapshot['status']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
