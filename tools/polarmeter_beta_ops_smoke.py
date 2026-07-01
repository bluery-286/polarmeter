#!/usr/bin/env python3
"""Operational smoke check for the public PolarMeter beta cache.

This intentionally checks the deployed Pages JSON, not the local fixture. It is
the guard for cases where local cache generation is healthy but the phone/web app
still sees an old public snapshot.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_BASE_URL = 'https://polarmeter.polarbearworks.com'
CORE_SIGNALS = {
    'sp500': 'S&P500 / SPY',
    'nasdaq100': 'Nasdaq100 / QQQ',
    'iwm': 'Russell2000 / IWM',
    'vix': 'VIX',
    'kospi': 'KOSPI',
    'kosdaq': 'KOSDAQ',
    'usd_krw': 'USD/KRW',
    'us10y': 'US 10Y',
    'wti': 'WTI',
}
REQUIRED_REFRESH_KEYS = {
    'kr_open_plus_30',
    'kr_open_plus_60',
    'kr_close_plus_15',
    'kr_close_plus_60',
    'us_open_plus_30',
    'us_open_plus_60',
    'us_close_plus_15',
    'us_close_plus_60',
}


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_minutes(value: Any, now: datetime) -> float | None:
    parsed = parse_iso(value)
    if not parsed:
        return None
    return (now - parsed).total_seconds() / 60


def fetch_json(base_url: str, name: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{name}"
    request = urllib.request.Request(url, headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
    with urllib.request.urlopen(request, timeout=20) as response:
        status = getattr(response, 'status', None) or 200
        if status != 200:
            raise AssertionError(f'{name} returned HTTP {status}')
        return json.loads(response.read().decode('utf-8'))


def in_window(now: datetime, zone_name: str, start: time, end: time) -> bool:
    local = now.astimezone(ZoneInfo(zone_name))
    if local.weekday() >= 5:
        return False
    current = local.time()
    return start <= current <= end


def signal_age_hours(signal: dict[str, Any], now: datetime) -> float | None:
    minutes = age_minutes(signal.get('dataAsOf'), now)
    if minutes is not None:
        return minutes / 60
    value = signal.get('dataAgeHours')
    return float(value) if isinstance(value, (int, float)) else None


def assert_public_cache(args: argparse.Namespace) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    health = fetch_json(args.base_url, 'health.json')
    manifest = fetch_json(args.base_url, 'market-snapshot-manifest.json')
    snapshot = fetch_json(args.base_url, 'market-snapshot-latest.json')

    errors: list[str] = []
    if health.get('ok') is not True:
        errors.append('health.json ok must be true')
    if manifest.get('snapshotStatus') not in {'ok', 'partial'}:
        errors.append(f"unexpected snapshotStatus: {manifest.get('snapshotStatus')}")
    generated_age = age_minutes(snapshot.get('generatedAt'), now)
    if generated_age is None:
        errors.append('snapshot generatedAt is missing or invalid')
    elif generated_age > args.max_cache_age_minutes:
        errors.append(f'public snapshot is stale: age={generated_age:.1f}m max={args.max_cache_age_minutes}m')
    if manifest.get('generatedAt') != snapshot.get('generatedAt'):
        errors.append('manifest generatedAt must match snapshot generatedAt')
    if not manifest.get('nextRefreshAt') or not manifest.get('marketDataNextRefreshAt'):
        errors.append('manifest must expose market data nextRefreshAt')
    if health.get('nextRefreshAt') != manifest.get('nextRefreshAt'):
        errors.append('health nextRefreshAt must match manifest nextRefreshAt')
    refresh_keys = {item.get('key') for item in manifest.get('criticalMarketRefreshes') or [] if isinstance(item, dict)}
    missing_refresh = REQUIRED_REFRESH_KEYS - refresh_keys
    if missing_refresh:
        errors.append(f'missing critical refresh keys: {sorted(missing_refresh)}')

    signals = snapshot.get('signals') or {}
    for key, label in CORE_SIGNALS.items():
        signal = signals.get(key)
        if not isinstance(signal, dict):
            errors.append(f'missing core signal: {label}')
            continue
        if signal.get('valuePolicy') != 'show':
            errors.append(f'{label} must be showable, got valuePolicy={signal.get("valuePolicy")}')
        if signal.get('status') not in {'ok', 'stale', 'suspect'}:
            errors.append(f'{label} status must be renderable, got {signal.get("status")}')

    if len((snapshot.get('news') or {}).get('items') or []) < args.min_news:
        errors.append(f'news item count below beta minimum {args.min_news}')

    kr_active = in_window(now, 'Asia/Seoul', time(9, 30), time(16, 40))
    us_active = in_window(now, 'America/New_York', time(9, 30), time(17, 30))
    if kr_active:
        for key in ['kospi', 'kosdaq', 'usd_krw']:
            age = signal_age_hours(signals.get(key) or {}, now)
            if age is None or age > args.active_market_signal_max_age_hours:
                errors.append(f'{CORE_SIGNALS[key]} is stale during KR market: age={age}')
    if us_active:
        for key in ['sp500', 'nasdaq100', 'iwm', 'vix']:
            age = signal_age_hours(signals.get(key) or {}, now)
            if age is None or age > args.active_market_signal_max_age_hours:
                errors.append(f'{CORE_SIGNALS[key]} is stale during US market: age={age}')

    core_signal_status = {
        key: {
            'label': label,
            'status': (signals.get(key) or {}).get('status'),
            'valuePolicy': (signals.get(key) or {}).get('valuePolicy'),
            'value': (signals.get(key) or {}).get('value'),
            'change': (signals.get(key) or {}).get('change'),
            'changePct': (signals.get(key) or {}).get('changePct'),
            'dataAsOf': (signals.get(key) or {}).get('dataAsOf'),
            'dataAgeHours': signal_age_hours(signals.get(key) or {}, now),
        }
        for key, label in CORE_SIGNALS.items()
    }

    summary = {
        'ok': not errors,
        'baseUrl': args.base_url,
        'now': now.isoformat().replace('+00:00', 'Z'),
        'generatedAt': snapshot.get('generatedAt'),
        'generatedAgeMinutes': generated_age,
        'nextRefreshAt': manifest.get('nextRefreshAt'),
        'snapshotStatus': manifest.get('snapshotStatus'),
        'newsCount': len((snapshot.get('news') or {}).get('items') or []),
        'krActive': kr_active,
        'usActive': us_active,
        'coreSignals': core_signal_status,
        'errors': errors,
    }
    if errors:
        raise AssertionError(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--max-cache-age-minutes', type=int, default=120)
    parser.add_argument('--active-market-signal-max-age-hours', type=float, default=3)
    parser.add_argument('--min-news', type=int, default=3)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    try:
        summary = assert_public_cache(args)
    except AssertionError as error:
        print('PolarMeter beta ops smoke: FAIL', file=sys.stderr)
        print(error, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            'PolarMeter beta ops smoke: PASS '
            f"generatedAt={summary['generatedAt']} age={summary['generatedAgeMinutes']:.1f}m "
            f"news={summary['newsCount']} next={summary['nextRefreshAt']}"
        )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
