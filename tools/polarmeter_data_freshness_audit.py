#!/usr/bin/env python3
"""Audit PolarMeter snapshot freshness selection.

This catches the failure mode where an older provider value marked "ok" wins
over a fresher candidate marked "suspect". For market-temperature UX, a stale
number presented as current is more damaging than a fresh value with a warning.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(__file__).resolve().parents[1]
PROJECT = WORKSPACE
DEFAULT_SNAPSHOT = WORKSPACE / 'testflight/free-cache-snapshot-latest.json'
DEFAULT_PROBE = WORKSPACE / 'testflight/free-provider-probe-report-latest.json'
RECENCY_GAP_HOURS = 12.0
MAX_SELECTED_AGE_HOURS = {
    'vix': 72.0,
}
RETIRED_SIGNAL_KEYS = {'kodex200', 'tiger200'}


def load_cache_module() -> Any:
    path = WORKSPACE / 'tools/polarmeter_cache_snapshot.py'
    spec = importlib.util.spec_from_file_location('polarmeter_cache_snapshot', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'failed to load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cache = load_cache_module()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


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


def selected_age(signal: dict[str, Any]) -> float | None:
    value = signal.get('dataAgeHours')
    if isinstance(value, (int, float)):
        return float(value)
    parsed = cache.parse_utc_datetime(signal.get('dataAsOf'))
    if not parsed:
        return None
    return cache.stale_signal_age_hours({'dataAsOf': signal.get('dataAsOf')})


def audit(snapshot: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    signals = snapshot.get('signals') if isinstance(snapshot.get('signals'), dict) else {}
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
        age = selected_age(signal)
        max_age = MAX_SELECTED_AGE_HOURS.get(key)
        if max_age is not None and (age is None or age > max_age):
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


def main() -> int:
    snapshot_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SNAPSHOT
    probe_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PROBE
    errors = audit(load_json(snapshot_path), load_json(probe_path))
    if errors:
        print('PolarMeter data freshness audit: FAIL')
        for error in errors:
            print(f'- {error}')
        return 1
    print('PolarMeter data freshness audit: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
