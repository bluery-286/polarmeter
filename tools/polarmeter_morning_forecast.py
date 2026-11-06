#!/usr/bin/env python3
"""Morning forecast (꽁치의 아침 예보): KOSPI open/close direction from the overnight US session.

Pure functions only. Bundle 1-A holds the inputs (SPEC §6.3 "입력") and the
prediction (SPEC §6.3 "계산"). The training script
(`polarmeter_forecast_train.py`) and the live worker layer (bundle 1-B) call the
same functions, so a backtested day and a live day are computed identically.

Words (SPEC §6.3):
  T  = target KR session (the day the forecast is about)
  P  = the KR session right before T
  US sessions = US trading days dated P <= d < T (exchange-local dates)

A US session dated d closes at 16:00 New York time (13:00 on early closes),
which is d+1 05:00/06:00 KST, so every session dated before T has closed by
T 09:00 KST. This module does not check whether the *latest* daily bar is
complete: Yahoo shows the live session as a bar while it trades. The caller
(1-B) must only pass sessions that have closed (SPEC §6.3 condition 2).

Freezing, scoring, the ledger and copy belong to bundle 1-B and are not here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import polarmeter_score_core_v3 as v3

MODEL_PATH = Path(__file__).with_name('polarmeter_forecast_model.json')
MODEL_VERSION = 'kospi-overnight-v1'
FEATURES = ('ewyExcessPct', 'spyPct', 'soxxPct')
US_INPUT_SYMBOLS = (('spyPct', 'SPY'), ('soxxPct', 'SOXX'), ('ewyPct', 'EWY'))
SESSION_SYMBOL = 'SPY'  # the US session list is SPY's dates; SOXX and EWY must match it
INPUT_DECIMALS = 2
PREDICTION_DECIMALS = 2


def load_model(path: Path | str = MODEL_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def us_sessions_between(us_dates: list[str], basis_kr_session: str, target_session: str) -> list[str]:
    """US session dates d with P <= d < T, oldest first."""
    return [d for d in us_dates if basis_kr_session <= d < target_session]


def cumulative_pct(series: v3.Series, sessions: list[str]) -> tuple[float | None, str | None]:
    """Cumulative % change over `sessions` and the base date it is measured from.

    The base is the bar right before the first session. Returns (None, None) if
    any session is missing from the series or any close on the way is missing.
    """
    if not sessions:
        return None, None
    first = series.index_on_or_before(sessions[0])
    if first < 1 or series.dates[first] != sessions[0]:
        return None, None
    last = first + len(sessions) - 1
    if last >= len(series.dates) or series.dates[first:last + 1] != list(sessions):
        return None, None
    window = series.closes[first - 1:last + 1]
    if any(close in (None, 0) for close in window):
        return None, None
    return (window[-1] / window[0] - 1) * 100, series.dates[first - 1]


def forecast_inputs(target_session: str, basis_kr_session: str, us_series: Mapping[str, v3.Series],
                    kospi_closes: v3.Series) -> dict[str, Any]:
    """Overnight inputs for target session T, measured after KR session P closed.

    Returns a dict with `usSessions`, `usBaseSession`, `inputs` (or None) and
    `reason` (None when the inputs are usable):
      us_market_closed     no US session dated P <= d < T
      us_series_missing    SPY/SOXX/EWY series not given
      us_session_mismatch  SOXX or EWY dates differ from SPY's in the window
      input_missing        a close needed for a change is missing
      kospi_prev_missing   a KOSPI close needed for `kospiPrevPct` is missing

    `kospiPrevPct` is KOSPI's cumulative change over the KR sessions dated after
    the EWY base bar (the US session right before the first one used) and up to
    P (SPEC §6.3, revised 2026-11-06). EWY's change from that base already
    carries those KR moves; usually it is P alone, after a US-only holiday it is
    two sessions. No such KR session gives 0.

    Inputs are rounded to 2 decimals before `ewyExcessPct` is formed, so the
    stored inputs reproduce the prediction exactly.
    """
    out: dict[str, Any] = {'targetSession': target_session, 'basisKrSession': basis_kr_session,
                           'usSessions': [], 'usBaseSession': None, 'krSessionsInEwy': [], 'inputs': None,
                           'reason': None}
    if any(symbol not in us_series or us_series[symbol] is None for _, symbol in US_INPUT_SYMBOLS):
        out['reason'] = 'us_series_missing'
        return out
    sessions = us_sessions_between(us_series[SESSION_SYMBOL].dates, basis_kr_session, target_session)
    out['usSessions'] = sessions
    if not sessions:
        out['reason'] = 'us_market_closed'
        return out
    raw: dict[str, float] = {}
    bases = set()
    for key, symbol in US_INPUT_SYMBOLS:
        series = us_series[symbol]
        if us_sessions_between(series.dates, basis_kr_session, target_session) != sessions:
            out['reason'] = 'us_session_mismatch'
            return out
        pct, base = cumulative_pct(series, sessions)
        if pct is None:
            out['reason'] = 'input_missing'
            return out
        raw[key] = round(pct, INPUT_DECIMALS)
        bases.add(base)
    if len(bases) != 1:
        out['reason'] = 'us_session_mismatch'
        return out
    out['usBaseSession'] = bases.pop()
    kospi_prev, kr_sessions = kospi_since(kospi_closes, out['usBaseSession'], basis_kr_session)
    out['krSessionsInEwy'] = kr_sessions
    if kospi_prev is None:
        out['reason'] = 'kospi_prev_missing'
        return out
    kospi_prev = round(kospi_prev, INPUT_DECIMALS)
    excess = raw['ewyPct'] - kospi_prev
    out['inputs'] = {'spyPct': raw['spyPct'], 'soxxPct': raw['soxxPct'], 'ewyPct': raw['ewyPct'],
                     'kospiPrevPct': kospi_prev, 'ewyExcessPct': round(excess, INPUT_DECIMALS)}
    return out


def kospi_since(kospi_closes: v3.Series, us_base: str, basis_kr_session: str) -> tuple[float | None, list[str]]:
    """KOSPI cumulative % over KR sessions d with us_base < d <= P, and those sessions."""
    if basis_kr_session not in kospi_closes.dates:
        return None, []
    kr_sessions = [d for d in kospi_closes.dates if us_base < d <= basis_kr_session]
    if not kr_sessions:
        return 0.0, []
    if kr_sessions[-1] != basis_kr_session:
        return None, kr_sessions  # P itself is missing
    first = kospi_closes.dates.index(kr_sessions[0])
    last = kospi_closes.dates.index(basis_kr_session)
    if first < 1:
        return None, kr_sessions  # no close before the window
    window = kospi_closes.closes[first - 1:last + 1]
    if any(close in (None, 0) for close in window):
        return None, kr_sessions
    return (window[-1] / window[0] - 1) * 100, kr_sessions


def linear(part: Mapping[str, Any], features: tuple[str, ...] | list[str], inputs: Mapping[str, float]) -> float:
    return float(part['intercept']) + sum(float(c) * float(inputs[name]) for c, name in zip(part['coef'], features))


def _sign(value: float) -> str:
    return 'down' if value < 0 else 'up' if value > 0 else 'none'


def analog_bucket(open_pct: float, model: Mapping[str, Any]) -> dict[str, Any] | None:
    """The analogBuckets row `open_pct` falls in. Down buckets are (lo, hi], up buckets [lo, hi)."""
    for bucket in model.get('analogBuckets') or []:
        lo, hi = bucket.get('lo'), bucket.get('hi')
        if hi is not None and hi <= 0:  # down side
            if (lo is None or open_pct > lo) and open_pct <= hi:
                return bucket
        elif lo is not None and lo >= 0:  # up side
            if open_pct >= lo and (hi is None or open_pct < hi):
                return bucket
    return None


def predict(inputs: Mapping[str, float], model: Mapping[str, Any]) -> dict[str, Any]:
    """Open/close % forecast and the calls, from the model's linear formulas.

    openPct/closePct are rounded to 2 decimals first and every call is made on
    the rounded value, so the published number and the call never disagree.
    """
    features = tuple(model['features'])
    open_pct = round(linear(model['open'], features, inputs), PREDICTION_DECIMALS)
    close_pct = round(linear(model['close'], features, inputs), PREDICTION_DECIMALS)
    call_threshold = float(model['open']['callThreshold'])
    strong_threshold = float(model['open']['strongThreshold'])
    strength = ('weak' if abs(open_pct) < call_threshold
                else 'moderate' if abs(open_pct) < strong_threshold else 'strong')
    open_call = 'none' if strength == 'weak' else _sign(open_pct)
    close_call = _sign(close_pct) if abs(close_pct) >= float(model['close']['callThreshold']) else 'none'
    contributions = {name: float(c) * float(inputs[name]) for c, name in zip(model['open']['coef'], features)}
    top_driver = max(features, key=lambda name: abs(contributions[name]))
    bucket = analog_bucket(open_pct, model) if open_call != 'none' else None
    return {'openPct': open_pct, 'closePct': close_pct, 'strength': strength, 'openCall': open_call,
            'closeCall': close_call, 'topDriver': top_driver,
            'analogBucket': bucket.get('id') if bucket else None}
