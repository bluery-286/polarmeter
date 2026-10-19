"""Bounded market-source diagnostics for operator logs, never public payloads."""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from polarmeter_last_close_policy import MAX_STALE_SIGNAL_AGE_HOURS, last_close_time

SIGNALS = frozenset(MAX_STALE_SIGNAL_AGE_HOURS) | {'kr_hynix'}
PROVIDERS = {'public-chart-delayed', 'twelvedata-free', 'fmp-free',
             'bok-ecos-free', 'data-go-kr-free', 'data-go-kr-index-free'}
STATUSES = {'ok', 'stale', 'suspect', 'partial', 'invalid', 'unavailable', 'error'}
REASON_PREFIXES = {
    'price_below_range<': 'price_below_range', 'price_above_range>': 'price_above_range',
    'changePct_out_of_range>': 'change_out_of_range', 'changePct_suspect>': 'large_move',
    'hard_stale ': 'too_old', 'active_market_stale ': 'active_market_too_old',
    'kr_intraday_prior_day_data ': 'prior_session_during_open',
}
REASONS = {'no_price', 'provider_error', 'provider_unavailable', 'missing_changePct',
           'invalid_last_close', 'invalid_vix_price', 'invalid_vix_changePct',
           'missing_or_invalid_vix_timestamp'}
REASONS |= {'invalid_rate_price', 'invalid_rate_changePct', 'missing_or_invalid_rate_timestamp'}


def safe_reason(value: object) -> str:
    if not isinstance(value, str):
        return 'unknown'
    value = value[:4096]
    if value in REASONS:
        return value
    for prefix, reason in REASON_PREFIXES.items():
        if value.startswith(prefix):
            return reason
    for code in (429, 401, 403, 404, 500, 502, 503, 504):
        if re.search(rf'\b{code}\b', value):
            return f'http_{code}'
    for name in ('TimeoutError', 'HTTPError', 'URLError', 'JSONDecodeError', 'ConnectionResetError'):
        if name in value:
            return name
    return 'unknown'


def _number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (ValueError, TypeError, OverflowError):
        return False


def _time_state(value: object, now: datetime, maximum: int | None) -> str:
    if value in (None, ''):
        return 'missing'
    observed = last_close_time({'dataAsOf': value}, now)
    if observed is None or observed > now:
        return 'invalid_or_future'
    age = (now - observed).total_seconds() / 3600
    return 'too_old' if maximum is not None and age > maximum else 'within_limit'


def summarize_market_sources(snapshot: object, probe: object, *, now: datetime | None = None) -> dict:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('signals'), dict):
        return {'reportStatus': 'snapshot_unreadable', 'signals': []}
    current = now or datetime.now(timezone.utc)
    providers = probe.get('providers') if isinstance(probe, dict) else []
    providers = providers if isinstance(providers, list) else []
    rows = []
    for key in sorted(SIGNALS):
        selected = snapshot['signals'].get(key)
        if not isinstance(selected, dict):
            continue
        missing = [name for name in ('value', 'changePct') if not _number(selected.get(name))]
        timing = _time_state(selected.get('dataAsOf'), current, MAX_STALE_SIGNAL_AGE_HOURS.get(key))
        if timing in {'missing', 'invalid_or_future'}:
            missing.append('dataAsOf')
        status = selected.get('status')
        if status == 'ok' and not missing and timing == 'within_limit':
            continue
        candidates = []
        chain = selected.get('fallbackChain')
        if isinstance(chain, list):
            for item in chain[:16]:
                if isinstance(item, dict):
                    candidates.append({'provider': item.get('provider'), 'status': item.get('status'),
                                       'quality': item.get('quality'), 'reason': item.get('reason')})
        for provider in providers[:16]:
            if not isinstance(provider, dict):
                continue
            items = provider.get('items')
            for item in items[:64] if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get('key') == key:
                    candidates.append({'provider': provider.get('provider'), 'status': item.get('status'),
                                       'quality': None, 'reason': item.get('reason')})
        safe_candidates, seen = [], set()
        for item in candidates:
            safe = {'provider': item['provider'] if isinstance(item['provider'], str) and item['provider'] in PROVIDERS else 'unknown',
                    'status': item['status'] if isinstance(item['status'], str) and item['status'] in STATUSES else 'unknown',
                    'quality': item['quality'] if isinstance(item['quality'], str) and item['quality'] in STATUSES else 'unknown',
                    'reason': safe_reason(item['reason'])}
            identity = tuple(safe.values())
            if identity not in seen:
                seen.add(identity)
                safe_candidates.append(safe)
            if len(safe_candidates) == 12:
                break
        rows.append({'key': key, 'status': status if isinstance(status, str) and status in STATUSES else 'unknown',
                     'missingFields': missing, 'observationTime': timing,
                     'maxAgeHours': MAX_STALE_SIGNAL_AGE_HOURS.get(key),
                     'selectedProvider': selected.get('provider') if isinstance(selected.get('provider'), str) and selected['provider'] in PROVIDERS else 'unknown',
                     'reason': safe_reason(selected.get('qualityReason')), 'candidates': safe_candidates})
    return {'reportStatus': 'available', 'signals': rows}


def emit_market_source_diagnostics(snapshot_path: Path, probe_path: Path) -> None:
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
        probe = json.loads(probe_path.read_text(encoding='utf-8'))
        summary = summarize_market_sources(snapshot, probe)
    except Exception:
        summary = {'reportStatus': 'unreadable', 'signals': []}
    print(json.dumps({'marketSourceDiagnostics': summary}, ensure_ascii=False), file=sys.stderr)


def summarize_market_freshness_failure(output: object) -> dict:
    """Classify the actual audit result, never repeat its untrusted prose.

    Source summaries intentionally omit healthy-looking quotes. The audit can
    still reject those quotes for candidate selection or active-market age, so
    its authoritative failure needs its own bounded, fixed-vocabulary record.
    """
    unreadable = {'reportStatus': 'freshness_audit_unreadable', 'signals': []}
    if not isinstance(output, str) or len(output) > 262144:
        return unreadable
    try:
        report = json.loads(output)
    except (ValueError, RecursionError):
        return unreadable
    if not isinstance(report, dict) or report.get('ok') is not False or not isinstance(report.get('errors'), list):
        return unreadable
    errors = report['errors']
    reasons_by_key = {}
    unclassified = not errors or len(errors) > 128
    for error in errors[:128]:
        if not isinstance(error, str) or len(error) > 4096:
            unclassified = True
            continue
        key, separator, detail = error.partition(': ')
        if not separator or key not in SIGNALS:
            unclassified = True
            continue
        reason = 'freshness_audit_failure'
        if detail.startswith('selected ') and ' is older than ' in detail:
            reason = 'provider_recency_gap'
        elif detail.startswith('active-market stale data must not be status=ok:'):
            reason = 'active_market_stale'
        elif detail.startswith('fresher public chart candidate exists but selected provider is '):
            reason = 'public_candidate_rank'
        reasons_by_key.setdefault(key, set()).add(reason)
    return {'reportStatus': 'freshness_audit_failed',
            'signals': [{'key': key, 'reasons': sorted(reasons)} for key, reasons in sorted(reasons_by_key.items())],
            'hasUnclassifiedFailure': unclassified}


def emit_market_freshness_failure(output: object) -> None:
    print(json.dumps({'marketSourceDiagnostics': summarize_market_freshness_failure(output)}), file=sys.stderr)
