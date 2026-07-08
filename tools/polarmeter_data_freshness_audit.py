#!/usr/bin/env python3
"""Audit PolarMeter snapshot freshness selection.

This catches the failure mode where an older provider value marked "ok" wins
over a fresher candidate marked "suspect". For market-temperature UX, a stale
number presented as current is more damaging than a fresh value with a warning.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT
PROJECT = WORKSPACE
DEFAULT_SNAPSHOT = WORKSPACE / 'testflight/free-cache-snapshot-latest.json'
DEFAULT_PROBE = WORKSPACE / 'testflight/free-provider-probe-report-latest.json'
RECENCY_GAP_HOURS = 12.0
MAX_SELECTED_AGE_HOURS = {
    'sp500': 72.0,
    'nasdaq100': 72.0,
    'iwm': 72.0,
    'soxx': 72.0,
    'smh': 72.0,
    'eem': 72.0,
    'vix': 72.0,
}
US_SESSION_SIGNAL_KEYS = {'sp500', 'nasdaq100', 'iwm', 'soxx', 'smh', 'eem', 'vix'}
ACTIVE_MARKET_MAX_AGE_HOURS = 3.0
KR_ACTIVE_MARKET_KEYS = {'kospi', 'kosdaq', 'usd_krw'}
US_ACTIVE_MARKET_KEYS = {'sp500', 'nasdaq100', 'iwm', 'soxx', 'smh', 'eem', 'vix'}
CRITICAL_SIGNAL_MAX_AGE_HOURS = {
    'sp500': 72.0,
    'nasdaq100': 72.0,
    'iwm': 72.0,
    'soxx': 72.0,
    'smh': 72.0,
    'eem': 72.0,
    'kospi': 96.0,
    'kosdaq': 96.0,
    'usd_krw': 72.0,
    'us10y': 96.0,
    'dxy': 96.0,
    'wti': 72.0,
    'gold': 96.0,
    'vix': 72.0,
}
CRITICAL_SIGNAL_REQUIRED_FIELDS = {
    'sp500': ('value', 'changePct', 'dataAsOf'),
    'nasdaq100': ('value', 'changePct', 'dataAsOf'),
    'iwm': ('value', 'changePct', 'dataAsOf'),
    'soxx': ('value', 'changePct', 'dataAsOf'),
    'smh': ('value', 'changePct', 'dataAsOf'),
    'eem': ('value', 'changePct', 'dataAsOf'),
    'kospi': ('value', 'changePct', 'dataAsOf'),
    'kosdaq': ('value', 'changePct', 'dataAsOf'),
    'usd_krw': ('value', 'changePct', 'dataAsOf'),
    'us10y': ('value', 'changePct', 'dataAsOf'),
    'dxy': ('value', 'changePct', 'dataAsOf'),
    'wti': ('value', 'changePct', 'dataAsOf'),
    'gold': ('value', 'changePct', 'dataAsOf'),
    'vix': ('value', 'changePct', 'dataAsOf'),
}
RETIRED_SIGNAL_KEYS = {'kodex200', 'tiger200'}


def load_cache_module() -> Any:
    path = WORKSPACE / 'tools/polarmeter_cache_snapshot.py'
    tools_dir = str(WORKSPACE / 'tools')
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    spec = importlib.util.spec_from_file_location('polarmeter_cache_snapshot', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'failed to load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_calendar_module() -> Any:
    path = WORKSPACE / 'tools/polarmeter_market_calendar.py'
    spec = importlib.util.spec_from_file_location('polarmeter_market_calendar', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'failed to load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cache = load_cache_module()
calendar = load_calendar_module()


def load_json(path: Path) -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as error:
            last_error = error
            time.sleep(0.2)
    if last_error:
        raise last_error
    raise RuntimeError(f'failed to read JSON: {path}')


def provider_candidates(probe: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider in probe.get('providers') or []:
        provider_name = str(provider.get('provider') or 'unknown')
        for item in provider.get('items') or []:
            if not isinstance(item, dict) or item.get('key') != key:
                continue
            quality, reason = cache.validate_candidate(key, item)
            if quality not in {'ok', 'suspect'}:
                continue
            rows.append({
                'provider': provider_name,
                'symbol': item.get('symbol'),
                'quality': quality,
                'reason': reason,
                'dataAsOf': cache.normalized_as_of(item.get('asOf')),
                'ageHours': cache.candidate_age_hours(item),
                'freshnessRank': cache.candidate_freshness_rank(item),
            })
    return rows


def selected_age(signal: dict[str, Any], as_of: datetime | None = None) -> float | None:
    value = signal.get('dataAgeHours')
    if as_of is None and isinstance(value, (int, float)):
        return float(value)
    parsed = cache.parse_utc_datetime(signal.get('dataAsOf'))
    if not parsed:
        return None
    now = as_of or datetime.now(timezone.utc)
    return max(0.0, (now - parsed).total_seconds() / 3600)


def snapshot_time(snapshot: dict[str, Any]) -> datetime:
    parsed = cache.parse_utc_datetime(snapshot.get('generatedAt'))
    return parsed or datetime.now(timezone.utc)


def is_us_trading_day(day: Any) -> bool:
    probe_time = datetime.combine(day, day_time(12, 0), tzinfo=calendar.NY_ZONE)
    return calendar.us_market_closed_reason(probe_time) is None


def last_completed_us_trading_date(as_of: datetime) -> Any:
    local = as_of.astimezone(calendar.NY_ZONE)
    candidate = local.date()
    # Give delayed/free providers a little time after the regular close before
    # requiring the current day's US close.
    if is_us_trading_day(candidate) and local.time() >= day_time(16, 45):
        return candidate
    candidate -= timedelta(days=1)
    while not is_us_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def tolerated_us_session_gap(key: str, signal: dict[str, Any], as_of: datetime, max_age: float | None) -> bool:
    if key not in US_SESSION_SIGNAL_KEYS or max_age is None:
        return False
    age = selected_age(signal, as_of)
    if age is None or age <= max_age:
        return False
    data_as_of = cache.parse_utc_datetime(signal.get('dataAsOf'))
    if data_as_of is None:
        return False
    data_date = data_as_of.astimezone(calendar.NY_ZONE).date()
    return data_date >= last_completed_us_trading_date(as_of)


def active_market_stale_ok_error(key: str, signal: dict[str, Any], as_of: datetime) -> str | None:
    status = str(signal.get('status') or '')
    if status != 'ok':
        return None
    if key in KR_ACTIVE_MARKET_KEYS:
        active = calendar.is_kr_market_active(as_of, day_time(9, 30), day_time(16, 40))
    elif key in US_ACTIVE_MARKET_KEYS:
        active = calendar.is_us_market_active(as_of, day_time(9, 30), day_time(17, 30))
    else:
        return None
    if not active:
        return None
    age = selected_age(signal, as_of)
    if age is None or age > ACTIVE_MARKET_MAX_AGE_HOURS:
        return f'{key}: active-market stale data must not be status=ok: age={age}, max={ACTIVE_MARKET_MAX_AGE_HOURS}'
    return None


def hard_stale_ok_error(key: str, signal: dict[str, Any], as_of: datetime, max_age: float | None) -> str | None:
    if max_age is None or str(signal.get('status') or '') != 'ok':
        return None
    age = selected_age(signal, as_of)
    if age is not None and age <= max_age:
        return None
    if tolerated_us_session_gap(key, signal, as_of, max_age):
        return None
    return f'{key}: too-old data must not be status=ok: age={age}, max={max_age}'


def field_missing(signal: dict[str, Any], field: str) -> bool:
    value = signal.get(field)
    if field in {'value', 'changePct'}:
        return value is None or cache.as_float(value) is None
    return value is None or str(value).strip() == ''


def audit(snapshot: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    signals = snapshot.get('signals') if isinstance(snapshot.get('signals'), dict) else {}
    as_of = snapshot_time(snapshot)
    for key, fields in CRITICAL_SIGNAL_REQUIRED_FIELDS.items():
        signal = signals.get(key)
        if not isinstance(signal, dict):
            errors.append(f'{key}: critical signal missing from snapshot')
            continue
        status = str(signal.get('status') or '')
        if status not in {'ok', 'stale', 'suspect'}:
            errors.append(f'{key}: critical signal status is not displayable: {status or "missing"}')
        for field in fields:
            if field_missing(signal, field):
                errors.append(f'{key}: critical signal missing {field}')
        age = selected_age(signal, as_of)
        max_age = CRITICAL_SIGNAL_MAX_AGE_HOURS.get(key)
        if max_age is not None and (age is None or age > max_age) and not tolerated_us_session_gap(key, signal, as_of, max_age):
            errors.append(f'{key}: critical signal too old: age={age}, max={max_age}')
        hard_error = hard_stale_ok_error(key, signal, as_of, max_age)
        if hard_error:
            errors.append(hard_error)
        active_error = active_market_stale_ok_error(key, signal, as_of)
        if active_error:
            errors.append(active_error)

    retired_in_snapshot = RETIRED_SIGNAL_KEYS.intersection(signals)
    if retired_in_snapshot:
        errors.append(f'retired domestic ETF proxy signals leaked into snapshot: {sorted(retired_in_snapshot)}')
    retired_required = [
        item.get('key') for item in (probe.get('requiredSignals') or [])
        if isinstance(item, dict) and item.get('key') in RETIRED_SIGNAL_KEYS
    ]
    if retired_required:
        errors.append(f'retired domestic ETF proxy signals still required by probe: {sorted(retired_required)}')
    for key, signal in signals.items():
        if not isinstance(signal, dict):
            continue
        age = selected_age(signal, as_of)
        max_age = MAX_SELECTED_AGE_HOURS.get(key)
        if max_age is not None and (age is None or age > max_age) and not tolerated_us_session_gap(key, signal, as_of, max_age):
            errors.append(f'{key}: selected data too old for fast-moving signal: age={age}, max={max_age}')

        candidates = provider_candidates(probe, key)
        fresher = [
            item for item in candidates
            if item.get('ageHours') is not None and age is not None and age - float(item['ageHours']) >= RECENCY_GAP_HOURS
        ]
        if fresher:
            best = min(fresher, key=lambda item: float(item['ageHours']))
            errors.append(
                f"{key}: selected {signal.get('provider')} {signal.get('dataAsOf')} "
                f"is older than {best['provider']} {best['dataAsOf']} by >= {RECENCY_GAP_HOURS:g}h"
            )

        if key in {'kospi', 'kosdaq'}:
            selected_provider = str(signal.get('provider') or '')
            selected_rank = signal.get('freshnessRank')
            public = [item for item in candidates if item.get('provider') == 'public-chart-delayed']
            if public:
                best_public = max(public, key=lambda item: (item.get('freshnessRank') or 0))
                if selected_provider != 'public-chart-delayed' and (best_public.get('freshnessRank') or 0) > (selected_rank or 0):
                    errors.append(f'{key}: fresher public chart candidate exists but selected provider is {selected_provider}')
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Audit PolarMeter snapshot freshness and critical signal coverage.')
    parser.add_argument('snapshot', nargs='?', type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument('probe', nargs='?', type=Path, default=DEFAULT_PROBE)
    parser.add_argument('--json', action='store_true', help='print machine-readable audit result')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors = audit(load_json(args.snapshot), load_json(args.probe))
    if args.json:
        print(json.dumps({'ok': not errors, 'errors': errors}, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    if errors:
        print('PolarMeter data freshness audit: FAIL')
        for error in errors:
            print(f'- {error}')
        return 1
    print('PolarMeter data freshness audit: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
