#!/usr/bin/env python3
"""Contract: one auxiliary signal without a change no longer stops every publish; core signals still do.

Also covers the transient-failure retry used by the quote fetchers.
Run: PYTHONPATH=tools python3 tools/polarmeter_partial_degrade_contract.py
"""
from __future__ import annotations

import copy
import io
import socket
import sys
import urllib.error
from contextlib import redirect_stderr

import polarmeter_data_freshness_audit as audit_module
import polarmeter_free_cache_worker as worker
from polarmeter_http_retry import is_transient, with_retry
from polarmeter_signal_tiers import AUX_SIGNAL_KEYS, CORE_SIGNAL_KEYS, PARTIAL_STREAK_LIMIT

GENERATED = '2026-11-10T03:00:00Z'  # Tuesday 12:00 KST: KR session open, US closed


def base_snapshot() -> dict:
    signals = {}
    for key in audit_module.CRITICAL_SIGNAL_REQUIRED_FIELDS:
        signals[key] = {'key': key, 'status': 'ok', 'value': 100.0, 'changePct': 0.5, 'dataAsOf': GENERATED,
                        'provider': 'contract'}
    return {'generatedAt': GENERATED, 'signals': signals, 'dataQuality': {}}


def run_audit(snapshot: dict) -> list[str]:
    return audit_module.audit(snapshot, {})


def quiet(call, *args):
    with redirect_stderr(io.StringIO()) as err:
        result = call(*args)
    return result, err.getvalue()


# 0. Tiers cover every audited signal exactly once.
assert CORE_SIGNAL_KEYS.isdisjoint(AUX_SIGNAL_KEYS)
assert set(audit_module.CRITICAL_SIGNAL_REQUIRED_FIELDS) == CORE_SIGNAL_KEYS | AUX_SIGNAL_KEYS
assert run_audit(base_snapshot()) == [], run_audit(base_snapshot())

# 1. The 11-09 shape: DXY has a value but no change. First publish: partial, audit passes, warning line.
snap = base_snapshot()
snap['signals']['dxy']['changePct'] = None
assert any('dxy: critical signal missing changePct' in e for e in run_audit(copy.deepcopy(snap))), \
    'without the worker annotation the audit stays strict'
keys, err = quiet(worker.apply_partial_signal_streaks, snap, {})
assert keys == ['dxy'] and snap['dataQuality']['partialSignals']['dxy']['streak'] == 1
assert '::warning::dxy' in err
assert run_audit(snap) == [], run_audit(snap)
assert audit_module.audit_warnings(snap) and 'streak=1' in audit_module.audit_warnings(snap)[0]
assert 'partialSignals' in worker.sanitize_public_data_quality(snap['dataQuality']), 'the count reaches the public snapshot'

# 2. Consecutive publishes count up from the previous public snapshot; at the limit the audit fails again.
previous = {'dataQuality': copy.deepcopy(snap['dataQuality'])}
for expected in range(2, PARTIAL_STREAK_LIMIT + 1):
    nxt = base_snapshot()
    nxt['signals']['dxy']['changePct'] = None
    quiet(worker.apply_partial_signal_streaks, nxt, previous)
    assert nxt['dataQuality']['partialSignals']['dxy']['streak'] == expected
    errors = run_audit(nxt)
    if expected < PARTIAL_STREAK_LIMIT:
        assert errors == [], errors
    else:
        assert errors == [f'dxy: critical signal missing changePct for {expected} publishes in a row'], errors
    previous = {'dataQuality': copy.deepcopy(nxt['dataQuality'])}

# 3. Recovery clears the count.
ok = base_snapshot()
ok['dataQuality'] = {'partialSignals': {'stale': 1}}
quiet(worker.apply_partial_signal_streaks, ok, previous)
assert 'partialSignals' not in ok['dataQuality']

# 4. Core signals stay strict: KOSPI without a change fails even with an annotation.
core = base_snapshot()
core['signals']['kospi']['changePct'] = None
keys, _ = quiet(worker.apply_partial_signal_streaks, core, {})
assert keys == []
core['dataQuality']['partialSignals'] = {'kospi': {'streak': 1}}
assert 'kospi: critical signal missing changePct' in run_audit(core)

# 5. An auxiliary signal without a value is still a failure (partial means value known, change unknown).
novalue = base_snapshot()
novalue['signals']['wti']['value'] = None
novalue['signals']['wti']['changePct'] = None
keys, _ = quiet(worker.apply_partial_signal_streaks, novalue, {})
assert keys == []
assert 'wti: critical signal missing value' in run_audit(novalue)

# 6. The failure summary names the reason in fixed words only.
from polarmeter_market_source_diagnostics import summarize_market_freshness_failure  # noqa: E402
summary = summarize_market_freshness_failure(
    '{"ok": false, "errors": ["kospi: critical signal missing changePct", "dxy: critical signal missing value"]}')
assert summary['signals'] == [{'key': 'dxy', 'reasons': ['missing_field']},
                              {'key': 'kospi', 'reasons': ['missing_change_pct']}], summary

# 7. Retry: transient errors are retried, others are not, and the attempt count is bounded.
def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError('https://example.invalid', code, 'x', {}, None)  # type: ignore[arg-type]


assert is_transient(http_error(429)) and is_transient(http_error(503)) and not is_transient(http_error(404))
assert is_transient(urllib.error.URLError(socket.timeout())) and is_transient(TimeoutError())
assert not is_transient(ValueError('bad json'))
waits: list[float] = []
calls = {'n': 0}


def flaky():
    calls['n'] += 1
    if calls['n'] < 3:
        raise http_error(429)
    return {'ok': True}


assert with_retry(flaky, sleep=waits.append) == {'ok': True} and calls['n'] == 3 and waits == [1.0, 3.0]
calls['n'] = 0


def always_down():
    calls['n'] += 1
    raise http_error(502)


try:
    with_retry(always_down, sleep=lambda _: None)
    raise AssertionError('should raise')
except urllib.error.HTTPError:
    assert calls['n'] == 3
calls['n'] = 0


def not_found():
    calls['n'] += 1
    raise http_error(404)


try:
    with_retry(not_found, sleep=lambda _: None)
    raise AssertionError('should raise')
except urllib.error.HTTPError:
    assert calls['n'] == 1, 'a 404 is not retried'

print('polarmeter partial degrade contract passed')
sys.exit(0)
