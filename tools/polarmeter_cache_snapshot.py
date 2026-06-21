#!/usr/bin/env python3
"""Build a provider-neutral free-cache snapshot for PolarMeter.

The snapshot is the cache-server contract. It can be built from a provider probe
report or, while keys are unavailable, as a safe placeholder that makes missing
coverage explicit without calling paid providers.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
PROJECT = WORKSPACE
DEFAULT_PROBE = PROJECT / 'testflight/free-provider-probe-report-latest.json'
DEFAULT_OUTPUT = PROJECT / 'testflight/free-cache-snapshot-latest.json'
DEFAULT_NEWS_PROBE = PROJECT / 'testflight/news-rss-probe-latest.json'
DEFAULT_LAST_KNOWN_GOOD = PROJECT / 'testflight/last-known-good-snapshot.json'
NEWS_RECOMMENDED_SCHEDULE = '30min_weekdays_60min_weekends_public_headline_cache'

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
    'kospi': {'minPrice': 1000, 'maxPrice': 12000, 'suspectAbsChangePct': 7.0, 'rejectAbsChangePct': 10.0, 'requiresChangePct': True},
    'kosdaq': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 10.0, 'requiresChangePct': True},
    # 2026 POC cross-check: KODEX/TIGER 200 ETF prices can trade above 120,000 KRW.
    'kodex200': {'minPrice': 20000, 'maxPrice': 200000, 'suspectAbsChangePct': 5.0, 'rejectAbsChangePct': 8.0, 'requiresChangePct': True},
    'tiger200': {'minPrice': 20000, 'maxPrice': 200000, 'suspectAbsChangePct': 5.0, 'rejectAbsChangePct': 8.0, 'requiresChangePct': True},
    'sp500': {'suspectAbsChangePct': 5.0, 'rejectAbsChangePct': 7.0, 'requiresChangePct': True},
    'nasdaq100': {'suspectAbsChangePct': 5.0, 'rejectAbsChangePct': 7.0, 'requiresChangePct': True},
    'usd_krw': {'suspectAbsChangePct': 2.0, 'rejectAbsChangePct': 3.0, 'requiresChangePct': False},
    'us10y': {'minPrice': 1.0, 'maxPrice': 8.0, 'suspectAbsChangePct': 8.0, 'rejectAbsChangePct': 15.0, 'requiresChangePct': False},
    'dxy': {'minPrice': 70.0, 'maxPrice': 140.0, 'suspectAbsChangePct': 3.0, 'rejectAbsChangePct': 5.0, 'requiresChangePct': False},
    'vix_aux': {'suspectAbsChangePct': 12.0, 'rejectAbsChangePct': 20.0, 'requiresChangePct': False},
    'wti': {'suspectAbsChangePct': 8.0, 'rejectAbsChangePct': 12.0, 'requiresChangePct': False},
    'gold': {'suspectAbsChangePct': 3.0, 'rejectAbsChangePct': 5.0, 'requiresChangePct': False},
    'soxx': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 9.0, 'requiresChangePct': False},
    'smh': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 9.0, 'requiresChangePct': False},
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


def provider_items(probe: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {provider.get('provider', 'unknown'): provider.get('items', []) for provider in probe.get('providers', [])}


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
    if key == 'usd_krw':
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
    if key in {'kospi', 'kosdaq'} and provider == 'public-chart-delayed':
        return {'sourceClass': 'worker_side_public_chart_intraday', 'displayBadge': kr_index_display_badge(data_as_of), 'confidencePolicy': 'normal_with_fallback_to_public_close'}
    if key in {'kospi', 'kosdaq'}:
        return {'sourceClass': 'delayed_market_data', 'displayBadge': '공공 지연', 'confidencePolicy': 'normal'}
    return {'sourceClass': 'delayed_market_data', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}


def normalize_signal(signal: dict[str, Any], provider: str, item: dict[str, Any], *, status: str = 'ok', reason: str | None = None) -> dict[str, Any]:
    key = signal['key']
    showable = status in {'ok', 'suspect'}
    data_as_of = normalized_as_of(item.get('asOf'))
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
        'fetchedAt': utc_now(),
        'ttlMinutes': previous.get('ttlMinutes') or 360,
    })
    return out


def choose_signal(signal: dict[str, Any], providers: dict[str, list[dict[str, Any]]], last_good: dict[str, Any]) -> dict[str, Any]:
    key = signal['key']
    failures: list[dict[str, Any]] = []
    partial_candidate: dict[str, Any] | None = None
    for provider, items in providers.items():
        for item in items:
            if item.get('key') != key:
                continue
            quality, reason = validate_candidate(key, item)
            failures.append({'provider': provider, 'status': item.get('status'), 'quality': quality, 'reason': reason or item.get('reason')})
            if quality == 'ok':
                out = normalize_signal(signal, provider, item, status='ok')
                out['fallbackChain'] = failures
                return out
            if quality == 'suspect' and partial_candidate is None:
                partial_candidate = normalize_signal(signal, provider, item, status='suspect', reason=reason)
            elif quality == 'partial' and partial_candidate is None:
                partial_candidate = normalize_signal(signal, provider, item, status='partial', reason=reason)

    if partial_candidate and partial_candidate.get('status') == 'suspect':
        partial_candidate['fallbackChain'] = failures
        return partial_candidate
    last_good_signal = stale_from_last_good(signal, last_good, failures[-1]['reason'] if failures else 'provider_unavailable')
    if last_good_signal:
        last_good_signal['fallbackChain'] = failures
        return last_good_signal
    if partial_candidate:
        partial_candidate['fallbackChain'] = failures
        return partial_candidate
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
    path.write_text(json.dumps({'updatedAt': utc_now(), 'signals': merged}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


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
                'sourceId': item.get('sourceId'),
                'region': item.get('region'),
                'provider': item.get('provider') or 'public-rss',
                'licenseNote': item.get('licenseNote') or 'public RSS headline cache only; no body or image scraping',
            }
            for item in items[:10]
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
        'defaultTtlMinutes': 30,
        'probeStatus': probe.get('status'),
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
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    write_last_known_good(args.last_known_good, snapshot)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(f"PolarMeter free cache snapshot: wrote {args.output} status={snapshot['status']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
