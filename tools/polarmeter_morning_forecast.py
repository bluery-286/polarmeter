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

Bundle 1-B (the section "Bundle 1-B" further down) adds freezing, scoring, the
ledger, the scorecard, the public copy and the worker entry point
`apply_morning_forecast`. It calls the 1-A functions above unchanged.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import polarmeter_market_calendar as cal
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


# ============================================================================
# Bundle 1-B: freeze, score, ledger, scorecard, copy, worker entry point
# (SPEC §6.3 freeze/score/ledger/pause, §6.4 public shape, §6.5 copy rules)
# ============================================================================

SCHEMA_VERSION = 'polarmeter-morning-forecast-v1'
KST = cal.KST_ZONE
NY = cal.NY_ZONE

LEDGER_SESSIONS = 60
BACKFILL_SESSIONS = 20
RECENT_SESSIONS = 20
MIN_ANALOG_DAYS = 20
EXACT_COUNT_MIN_DAYS = 100   # plan S7: below this the past line says a rough share
KO_COUNT_WORDS = ('', '하나', '둘', '셋', '넷', '다섯', '여섯', '일곱', '여덟', '아홉')
SUSPECT_ABS_PCT = 15.0
VOID_AFTER_TRADING_DAYS = 3
PAUSE_MIN_CALLS = 10
PAUSE_BELOW_HIT_RATE = 0.6
OPEN_TIME_KST = time(9, 0)
CLOSE_TIME_KST = time(15, 30)
ISSUE_DEADLINE_KST = time(8, 50)   # SPEC §6.3 condition 3
OPEN_SCORE_TIME_KST = time(9, 5)   # SPEC §6.3 scoring
CLOSE_SCORE_TIME_KST = time(15, 40)
US_READY_MINUTES = 15              # SPEC §6.3 condition 2: 15 minutes after the US close
US_FEED_GRACE_MINUTES = 45         # `last_completed_session_date(.., 'US')` reports a day 45 minutes after its close

STATES = ('pending', 'issued', 'open_scored', 'closed_scored', 'no_forecast', 'void')
SCORED_STATES = ('open_scored', 'closed_scored')
NO_FORECAST_REASONS = ('us_market_closed', 'missed_window', 'inputs_suspect', 'data_unavailable')
STATUSES = ('ok', 'paused', 'unavailable')
PHASES = ('pre_open', 'kr_open', 'kr_closed', 'us_open', 'off')
DISCLAIMER = {'ko': '과거 기록이며 앞으로의 결과를 보장하지 않습니다. 투자 권유가 아닙니다.',
              'en': 'Past record only; it does not guarantee future results. This is not investment advice.'}
ROW_KEYS = ('targetSession', 'basisKrSession', 'usSessions', 'origin', 'state', 'issuedAt', 'noForecastReason',
            'inputs', 'predicted', 'analog', 'actual', 'result')

Fetch = Callable[[str], Any]


class ForecastUnavailable(Exception):
    """The layer cannot run at all (not a per-day condition). The message is a short public reason."""


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def as_aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- calendar

def kr_open_day(day: date) -> bool:
    if day.year not in cal.KR_CALENDAR_YEARS:
        raise ForecastUnavailable('kr_calendar_unsupported')
    return cal.kr_market_closed_reason(datetime.combine(day, time(12), KST)) is None


def kr_day_after(day: date) -> date:
    for _ in range(20):
        day += timedelta(days=1)
        if kr_open_day(day):
            return day
    raise ForecastUnavailable('kr_calendar_unsupported')


def kr_day_before(day: date) -> date:
    for _ in range(20):
        day -= timedelta(days=1)
        if kr_open_day(day):
            return day
    raise ForecastUnavailable('kr_calendar_unsupported')


def trading_days_back(day: date, count: int) -> list[date]:
    """Up to `count` KR trading days before `day`, newest first; stops quietly where the holiday table ends."""
    out: list[date] = []
    try:
        while len(out) < count:
            day = kr_day_before(day)
            out.append(day)
    except ForecastUnavailable:
        pass
    return out


def target_session_for(now: datetime) -> date:
    """T: today if today is a KR trading day and it is before 09:00 KST, else the next KR trading day."""
    kst = as_aware(now).astimezone(KST)
    today = kst.date()
    if kr_open_day(today) and kst.time() < OPEN_TIME_KST:
        return today
    return kr_day_after(today)


def kr_days_elapsed(target: date, today: date) -> int:
    """KR trading days d with target < d <= today."""
    if (today - target).days > 40:
        return 99
    count, day = 0, target
    while day < today:
        day += timedelta(days=1)
        if kr_open_day(day):
            count += 1
    return count


def us_calendar_sessions(basis: str, target: str) -> list[str]:
    """US trading days d with P <= d < T by the exchange calendar (not by what Yahoo returned)."""
    out, day, end = [], date.fromisoformat(basis), date.fromisoformat(target)
    while day < end:
        if cal.us_market_closed_reason(datetime.combine(day, time(12), NY)) is None:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def us_session_close_at(day: str) -> datetime:
    d = date.fromisoformat(day)
    return datetime.combine(d, cal.us_equity_close_time(datetime.combine(d, time(12), NY)), NY)


def us_session_ready_at(day: str) -> datetime:
    """Earliest moment a forecast that needs US session `day` may be issued (SPEC §6.3 condition 2)."""
    return us_session_close_at(day) + timedelta(minutes=max(US_READY_MINUTES, US_FEED_GRACE_MINUTES))


def issue_deadline(target: str) -> datetime:
    return datetime.combine(date.fromisoformat(target), ISSUE_DEADLINE_KST, KST)


def ready_at_for(basis: str, target: str) -> datetime:
    """When T's forecast (or its 'US market closed' answer) becomes available."""
    sessions = us_calendar_sessions(basis, target)
    return us_session_ready_at(sessions[-1] if sessions else basis)


# ---------------------------------------------------------------- numbers

def sign_name(value: float | None) -> str | None:
    if value is None:
        return None
    return 'down' if value < 0 else 'up' if value > 0 else None


def call_hit(call: str | None, actual_pct: float | None) -> bool | None:
    """None when no call was made or nothing to compare; an exact 0 is a miss."""
    if call in (None, 'none') or actual_pct is None:
        return None
    return sign_name(actual_pct) == call


def input_is_suspect(inputs: Mapping[str, float]) -> bool:
    return any(abs(float(inputs[name])) > SUSPECT_ABS_PCT for name in FEATURES)


def open_band(open_pct: float, strength: str, model: Mapping[str, Any]) -> list[float] | None:
    bands = model.get('openBands') or []
    index = {'weak': 0, 'moderate': 1, 'strong': 2}[strength]
    band = bands[index] if index < len(bands) else {}
    err = band.get('p80AbsErr') or band.get('p50AbsErr')   # plan S7: 8 of 10 past opens inside, not half
    if err is None:
        return None
    return [round(open_pct - err, 2), round(open_pct + err, 2)]


def analog_record(open_call: str, bucket_id: str | None, model: Mapping[str, Any]) -> dict[str, Any] | None:
    """Past record of days like this one. Widens toward zero until at least 20 days (SPEC §4.4)."""
    if open_call == 'none' or not bucket_id:
        return None
    down = open_call == 'down'
    side = [b for b in model.get('analogBuckets') or [] if (b.get('hi') is not None and b['hi'] <= 0) == down]
    ids = [b['id'] for b in side]  # model order: down strongest-first, up weakest-first
    if bucket_id not in ids:
        return None
    start = ids.index(bucket_id)
    ladder = [side[start:start + k] if down else side[max(0, start - k + 1):start + 1]
              for k in range(1, len(side) + 1)]
    ladder = [group for i, group in enumerate(ladder) if i == 0 or len(group) > len(ladder[i - 1])]
    walk = model.get('walkForward') or {}
    period = f"{str(walk.get('from', ''))[:7]} ~ {str(walk.get('to', ''))[:7]}"
    chosen = None
    for group in ladder:
        days = sum(int(b.get('days') or 0) for b in group)
        chosen = {'bucket': bucket_id, 'days': days,
                  'openSame': sum(int(b.get('openDown' if down else 'openUp') or 0) for b in group),
                  'closeSame': sum(int(b.get('closeDown' if down else 'closeUp') or 0) for b in group),
                  'period': period, 'widened': len(group) > 1}
        if days >= MIN_ANALOG_DAYS:
            break
    return chosen


def predicted_block(inputs: Mapping[str, float], model: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    pred = predict(inputs, model)
    bucket_id = pred.pop('analogBucket')
    block = {'openPct': pred['openPct'], 'closePct': pred['closePct'],
             'openBand': open_band(pred['openPct'], pred['strength'], model), 'strength': pred['strength'],
             'openCall': pred['openCall'], 'closeCall': pred['closeCall'], 'topDriver': pred['topDriver']}
    return block, analog_record(pred['openCall'], bucket_id, model)


# ---------------------------------------------------------------- rows

def new_row(target: str, basis: str, origin: str) -> dict[str, Any]:
    return {'targetSession': target, 'basisKrSession': basis, 'usSessions': [], 'origin': origin, 'state': 'pending',
            'issuedAt': None, 'noForecastReason': None, 'inputs': None, 'predicted': None, 'analog': None,
            'actual': None, 'result': None}


def make_no_forecast(row: dict[str, Any], reason: str, sessions: list[str]) -> dict[str, Any]:
    row.update(state='no_forecast', noForecastReason=reason, usSessions=list(sessions))
    return row


def make_issued(row: dict[str, Any], fi: Mapping[str, Any], model: Mapping[str, Any], issued_at: str | None,
                paused: bool = False) -> dict[str, Any]:
    predicted, analog = predicted_block(fi['inputs'], model)
    row.update(state='issued', issuedAt=issued_at, usSessions=list(fi['usSessions']), inputs=dict(fi['inputs']),
               predicted=predicted, analog=analog, modelVersion=model.get('modelVersion'))
    if len(fi.get('krSessionsInEwy') or []) >= 2:
        row['afterUsHoliday'] = True
    if paused:
        row['paused'] = True
    return row


def row_from_data(target: str, basis: str, us_series: Mapping[str, v3.Series], kospi: v3.Series, model: Mapping[str, Any],
                  origin: str, issued_at: str | None, paused: bool = False) -> dict[str, Any]:
    """Compute a row from already-known data (backfill and gap days): no waiting, no time window."""
    row = new_row(target, basis, origin)
    sessions = us_calendar_sessions(basis, target)
    if not sessions:
        return make_no_forecast(row, 'us_market_closed', [])
    fi = forecast_inputs(target, basis, us_series, kospi)
    if fi['inputs'] is None:
        return make_no_forecast(row, 'data_unavailable', sessions)
    if input_is_suspect(fi['inputs']):
        return make_no_forecast(row, 'inputs_suspect', sessions)
    return make_issued(row, fi, model, issued_at, paused)


def frozen_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """What may never change once a row was issued (SPEC §6.3). `inputs` count only where both sides have them."""
    return {key: deepcopy(row.get(key)) for key in ('origin', 'issuedAt', 'predicted', 'analog', 'usSessions',
                                                    'basisKrSession', 'afterUsHoliday', 'paused')}


def valid_row(row: Any) -> bool:
    return (isinstance(row, dict) and isinstance(row.get('targetSession'), str)
            and re.fullmatch(r'\d{4}-\d{2}-\d{2}', row['targetSession']) is not None
            and isinstance(row.get('basisKrSession'), str) and row.get('state') in STATES
            and row.get('origin') in ('live', 'backfill'))


def load_previous(previous_public: Mapping[str, Any] | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    """Ledger rows by target day (the current row's inputs restored), the carried live totals, the previous block."""
    block = (previous_public or {}).get('morningForecast')
    rows: dict[str, dict[str, Any]] = {}
    carried: dict[str, Any] = {}
    if not isinstance(block, dict) or block.get('schemaVersion') != SCHEMA_VERSION:
        return rows, carried, None
    for item in block.get('ledger') or []:
        if valid_row(item):
            rows[item['targetSession']] = deepcopy(item)
    current = block.get('current')
    if valid_row(current):
        base = rows.setdefault(current['targetSession'], {k: deepcopy(v) for k, v in current.items() if k != 'copy'})
        if isinstance(current.get('inputs'), dict):
            base['inputs'] = deepcopy(current['inputs'])
    live = ((block.get('scorecard') or {}).get('live') or {}).get('carried')
    if isinstance(live, dict):
        carried = {'since': live.get('since'), 'calls': int(live.get('calls') or 0), 'openHits': int(live.get('openHits') or 0)}
    return rows, carried, block


# ---------------------------------------------------------------- issue decision

def issue_decision(target: date, now: datetime, us_series: Mapping[str, v3.Series], kospi: v3.Series,
                   model: Mapping[str, Any]) -> dict[str, Any]:
    """What to do about T's row right now: wait, record no_forecast, or issue (SPEC §6.3 conditions 1-5).

    Returns {'action': 'wait'|'no_forecast'|'issue', ...}. Condition 5 (no row yet / pending) is the caller's.
    """
    t, p = target.isoformat(), kr_day_before(target).isoformat()
    sessions = us_calendar_sessions(p, t)
    if not sessions:                                                    # condition 1
        if now >= us_session_ready_at(p):
            return {'action': 'no_forecast', 'reason': 'us_market_closed', 'basis': p, 'sessions': []}
        return {'action': 'wait', 'basis': p}
    if now >= issue_deadline(t):                                        # condition 3
        return {'action': 'no_forecast', 'reason': 'missed_window', 'basis': p, 'sessions': sessions}
    last = sessions[-1]
    completed = cal.last_completed_session_date(now, 'US')              # condition 2
    if now < us_session_ready_at(last) or completed is None or completed.isoformat() != last:
        return {'action': 'wait', 'basis': p}
    fi = forecast_inputs(t, p, us_series, kospi)                        # condition 4
    if fi['inputs'] is None:
        return {'action': 'wait', 'basis': p, 'detail': fi['reason']}
    if input_is_suspect(fi['inputs']):
        return {'action': 'no_forecast', 'reason': 'inputs_suspect', 'basis': p, 'sessions': sessions}
    return {'action': 'issue', 'basis': p, 'fi': fi}


# ---------------------------------------------------------------- scoring

def score_row(row: dict[str, Any], prices: Mapping[str, Mapping[str, Any]], now: datetime) -> None:
    """Fill `actual` (open after 09:05, close after 15:40) and advance an issued row's state. Never touches the frozen fields."""
    target, basis = row['targetSession'], row['basisKrSession']
    day = date.fromisoformat(target)
    kst = now.astimezone(KST)
    today_prices, basis_prices = prices.get(target), prices.get(basis)
    prev_close = (basis_prices or {}).get('close')
    actual = dict(row.get('actual') or {'openPct': None, 'closePct': None, 'intradayPct': None, 'source': None})
    if today_prices and prev_close:
        open_price, close_price = today_prices.get('open'), today_prices.get('close')
        if actual.get('openPct') is None and open_price and kst >= datetime.combine(day, OPEN_SCORE_TIME_KST, KST):
            actual['openPct'] = round((open_price / prev_close - 1) * 100, 2)
            actual['source'] = today_prices.get('source')
        if actual.get('closePct') is None and close_price and kst >= datetime.combine(day, CLOSE_SCORE_TIME_KST, KST):
            actual['closePct'] = round((close_price / prev_close - 1) * 100, 2)
            actual['intradayPct'] = round((close_price / open_price - 1) * 100, 2) if open_price else None
            actual['source'] = actual.get('source') or today_prices.get('source')
            if today_prices.get('source') != actual['source']:
                actual['closeSource'] = today_prices.get('source')
    if any(actual.get(k) is not None for k in ('openPct', 'closePct')):
        row['actual'] = actual
    if row['state'] == 'issued' and actual.get('openPct') is not None:
        pred = row['predicted']
        band = pred.get('openBand')
        in_band = (None if pred['openCall'] == 'none' or not band
                   else band[0] <= actual['openPct'] <= band[1])
        row['result'] = {'openHit': call_hit(pred['openCall'], actual['openPct']), 'closeHit': None, 'openInBand': in_band}
        row['state'] = 'open_scored'
    if row['state'] == 'open_scored' and actual.get('closePct') is not None:
        row['result']['closeHit'] = call_hit(row['predicted']['closeCall'], actual['closePct'])
        row['state'] = 'closed_scored'


def settle_unscorable(row: dict[str, Any], today: date) -> None:
    """3 KR trading days without an official number: an unscored issued row is void; a missing close is left unscored."""
    if row['state'] not in ('issued', 'open_scored'):
        return
    if kr_days_elapsed(date.fromisoformat(row['targetSession']), today) < VOID_AFTER_TRADING_DAYS:
        return
    if row['state'] == 'issued':
        row['state'] = 'void'
    else:
        row['result']['closeHit'] = None
        row['result']['closeUnscored'] = True
        row['state'] = 'closed_scored'


def fill_actual_only(row: dict[str, Any], prices: Mapping[str, Mapping[str, Any]], now: datetime) -> None:
    """no_forecast rows keep their state but still get the day's real open/close for the record."""
    state, predicted = row['state'], row.get('predicted')
    row['state'] = 'issued'
    row['predicted'] = {'openCall': 'none', 'closeCall': 'none'}
    try:
        score_row(row, prices, now)
    finally:
        row['state'], row['predicted'], row['result'] = state, predicted, None


# ---------------------------------------------------------------- scorecard

def row_mark(row: Mapping[str, Any]) -> str:
    hit = (row.get('result') or {}).get('openHit')
    return 'hit' if hit is True else 'miss' if hit is False else 'none'


def recent_window(rows: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    eligible = [rows[key] for key in sorted(rows) if rows[key]['state'] in SCORED_STATES + ('no_forecast',)]
    return eligible[-RECENT_SESSIONS:]


def is_paused(rows: Mapping[str, Mapping[str, Any]]) -> bool:
    window = recent_window(rows)
    marks = [row_mark(r) for r in window]
    calls = marks.count('hit') + marks.count('miss')
    return calls >= PAUSE_MIN_CALLS and marks.count('hit') / calls < PAUSE_BELOW_HIT_RATE


def window_counts(window: list[Mapping[str, Any]]) -> dict[str, Any]:
    marks = [row_mark(r) for r in window]
    return {'sessions': len(window), 'calls': marks.count('hit') + marks.count('miss'), 'openHits': marks.count('hit'),
            'marks': marks}


def build_scorecard(rows: Mapping[str, Mapping[str, Any]], carried: Mapping[str, Any], model: Mapping[str, Any]) -> dict[str, Any]:
    window = recent_window(rows)
    marks = [row_mark(r) for r in window]
    close_results = [(r.get('result') or {}).get('closeHit') for r in window]
    live = [r for r in rows.values() if r.get('origin') == 'live' and r['state'] in SCORED_STATES]
    live_marks = [row_mark(r) for r in live]
    carried_calls, carried_hits = int(carried.get('calls') or 0), int(carried.get('openHits') or 0)
    firsts = [r['targetSession'] for r in rows.values() if r.get('origin') == 'live' and r['state'] != 'pending']
    since_candidates = [s for s in [carried.get('since')] + firsts if s]
    walk = model.get('walkForward') or {}
    return {
        'recent': {'sessions': len(window), 'calls': marks.count('hit') + marks.count('miss'), 'openHits': marks.count('hit'),
                   'closeCalls': sum(c is not None for c in close_results), 'closeHits': sum(c is True for c in close_results),
                   'liveSessions': sum(r.get('origin') == 'live' for r in window), 'marks': marks},
        # plan S7: the same window split by origin, so no screen counts a recomputed day as a real forecast
        'recentLive': window_counts([r for r in window if r.get('origin') == 'live']),
        'recentBacktest': window_counts([r for r in window if r.get('origin') != 'live']),
        'live': {'since': min(since_candidates) if since_candidates else None,
                 'calls': carried_calls + live_marks.count('hit') + live_marks.count('miss'),
                 'openHits': carried_hits + live_marks.count('hit'),
                 'carried': {'since': carried.get('since'), 'calls': carried_calls, 'openHits': carried_hits}},
        'backtest': {'from': walk.get('from'), 'to': walk.get('to'), 'days': walk.get('days'), 'calls': walk.get('calls'),
                     'openHits': walk.get('openHits'), 'noCallDays': walk.get('noCallDays')},
    }


def trim_ledger(rows: dict[str, dict[str, Any]], carried: dict[str, Any]) -> dict[str, Any]:
    """Keep the newest 60 rows. A live, scored row that falls out is added to the carried live totals."""
    keep = set(sorted(rows)[-LEDGER_SESSIONS:])
    carried = dict(carried) if carried else {'since': None, 'calls': 0, 'openHits': 0}
    for key in sorted(rows):
        if key in keep:
            continue
        row = rows.pop(key)
        if row.get('origin') == 'live' and row['state'] in SCORED_STATES:
            carried['since'] = min(filter(None, [carried.get('since'), key]))
            mark = row_mark(row)
            carried['calls'] = int(carried.get('calls') or 0) + (mark in ('hit', 'miss'))
            carried['openHits'] = int(carried.get('openHits') or 0) + (mark == 'hit')
    return carried


def next_issue_at(rows: Mapping[str, Any], target: date) -> str:
    """Earliest moment the next not-yet-recorded forecast can be issued (the real run follows on the next workflow run)."""
    open_target = kr_day_after(target) if target.isoformat() in rows else target
    basis = kr_day_before(open_target)
    return iso_utc(ready_at_for(basis.isoformat(), open_target.isoformat()))


def phase_for(current: Mapping[str, Any] | None, now: datetime) -> str:
    if current is None:
        return 'off'
    context = cal.market_session_context(now, 'US') or {}
    if context.get('phase') == 'open':
        return 'us_open'
    kst = now.astimezone(KST)
    target = date.fromisoformat(current['targetSession'])
    if target > kst.date():
        return 'pre_open'
    if target < kst.date():
        return 'kr_closed'
    return 'pre_open' if kst.time() < OPEN_TIME_KST else 'kr_open' if kst.time() < CLOSE_TIME_KST else 'kr_closed'


# ---------------------------------------------------------------- copy (SPEC §4.3-§4.5, §6.5)

MINUS = '−'
WEEKDAYS_KO = '월화수목금토일'
WEEKDAYS_EN = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
MONTHS_EN = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
DIRECTION_KO = {'down': '하락', 'up': '상승', 'none': '방향 없음'}
DIRECTION_EN = {'down': 'lower', 'up': 'higher', 'none': 'no call'}


def fmt_num(value: float, decimals: int = 1, plus: bool = False) -> str:
    rounded = round(value, decimals)
    text = f'{abs(rounded):,.{decimals}f}'
    if rounded == 0:
        return text
    return (MINUS if rounded < 0 else '+' if plus else '') + text


def fmt_pct(value: float, decimals: int = 1, plus: bool = False) -> str:
    return fmt_num(value, decimals, plus) + '%'


def round_half(value: float, step: float = 0.5) -> float:
    """Nearest 0.5, ties away from zero (so +3.75 and -3.75 round alike)."""
    return math.copysign(math.floor(abs(value) / step + 0.5), value) * step


def fmt_half(value: float, plus: bool = False) -> str:
    rounded = round_half(value)
    if rounded == 0:
        return '0'
    return (MINUS if rounded < 0 else '+' if plus else '') + f'{abs(rounded):g}'


def day_words(target: str, today: date) -> tuple[str, str]:
    """('오늘' | 'Monday'-style weekday | past date) in Korean and English (SPEC §4.3: non-today targets use the weekday)."""
    day = date.fromisoformat(target)
    if day == today:
        return '오늘', 'today'
    if day > today:
        return f'{WEEKDAYS_KO[day.weekday()]}요일', WEEKDAYS_EN[day.weekday()]
    return f'{day.month}월 {day.day}일', f'{MONTHS_EN[day.month - 1]} {day.day}'


def tone_of(call: str) -> str:
    return 'minus' if call == 'down' else 'plus' if call == 'up' else 'neutral'


def title_copy(row: Mapping[str, Any], today: date) -> dict[str, str]:
    ko_day, en_day = day_words(row['targetSession'], today)
    prefix = '' if en_day == 'today' else f'{en_day}: '
    if row['state'] == 'no_forecast':
        if row.get('noForecastReason') == 'us_market_closed':
            return {'ko': '밤사이 미국장이 쉬어서 예보가 없어요', 'en': 'No forecast: US markets were closed'}
        return {'ko': f'자료가 덜 들어와 {ko_day} 예보를 내지 못했어요', 'en': 'No forecast: data arrived too late'}
    if row.get('paused'):
        return {'ko': '요즘 신호가 잘 안 맞아 예보를 쉬어요', 'en': 'Forecast paused: the signal has been off lately'}
    pred = row['predicted']
    if pred['openCall'] == 'none':
        return {'ko': f'{ko_day}은 방향을 내지 않아요' if ko_day != '오늘' else '오늘은 방향을 내지 않아요',
                'en': 'No call today' if en_day == 'today' else f'No call for {en_day}'}
    strong = pred['strength'] == 'strong'
    if pred['openCall'] == 'down':
        return {'ko': f'{ko_day} 한국장, ' + ('크게 밀려서 시작할 것으로 보여요' if strong else '내려서 시작할 가능성이 높아요'),
                'en': prefix + ('Korea looks set to open sharply lower' if strong else 'Korea is likely to open lower')}
    return {'ko': f'{ko_day} 한국장, ' + ('크게 올라서 시작할 것으로 보여요' if strong else '올라서 시작할 가능성이 높아요'),
            'en': prefix + ('Korea looks set to open sharply higher' if strong else 'Korea is likely to open higher')}


def badge_copy(row: Mapping[str, Any]) -> dict[str, str]:
    if row['state'] == 'no_forecast':
        return {'ko': '예보 없음', 'en': 'No forecast', 'tone': 'neutral'}
    if row.get('paused'):
        return {'ko': '예보 쉬는 중', 'en': 'Paused', 'tone': 'neutral'}
    pred = row['predicted']
    ko, en = {'strong': ('신호 뚜렷', 'Strong signal'), 'moderate': ('신호 보통', 'Moderate signal'),
              'weak': ('신호 약함', 'Weak signal')}[pred['strength']]
    return {'ko': ko, 'en': en, 'tone': tone_of(pred['openCall'])}


def chip_tone(value: float) -> str:
    return 'minus' if value <= -0.5 else 'plus' if value >= 0.5 else 'neutral'


def chips_copy(inputs: Mapping[str, float]) -> list[dict[str, str]]:
    """Three overnight numbers. The third is the Korea ETF's move beyond KOSPI's own (the model's strongest input)."""
    spy, soxx, excess = float(inputs['spyPct']), float(inputs['soxxPct']), float(inputs['ewyExcessPct'])
    return [
        {'ko': f'S&P500 {fmt_pct(spy, 1, True)}', 'en': f'S&P 500 {fmt_pct(spy, 1, True)}', 'tone': chip_tone(spy)},
        {'ko': f'미국 반도체 {fmt_pct(soxx, 1, True)}', 'en': f'US chips {fmt_pct(soxx, 1, True)}', 'tone': chip_tone(soxx)},
        {'ko': f'한국 ETF 코스피 대비 {fmt_num(excess, 1, True)}%p', 'en': f'Korea ETF vs KOSPI {fmt_num(excess, 1, True)}pp',
         'tone': chip_tone(excess)},
    ]


def past_copy(row: Mapping[str, Any]) -> dict[str, str] | None:
    analog, pred = row.get('analog'), row['predicted']
    if not analog or pred['openCall'] == 'none' or int(analog.get('days') or 0) < MIN_ANALOG_DAYS:
        return None
    since = str(analog.get('period', '')).split(' ~ ')[0]
    try:
        year, month = int(since[:4]), int(since[5:7])
    except ValueError:
        return None
    down = pred['openCall'] == 'down'
    days, same = int(analog['days']), int(analog['openSame'])
    if days < EXACT_COUNT_MIN_DAYS:
        # plan S7: a small sample reads as a rough share, so "57일 중 54일" does not sound more precise than it is
        tenths = round(10 * same / days)
        share_ko = '거의 매번' if tenths >= 10 else f'열에 {KO_COUNT_WORDS[tenths]}쯤' if tenths >= 1 else '열에 하나도 안 되게'
        share_en = 'Nearly every one' if tenths >= 10 else f'About {tenths} in 10' if tenths >= 1 else 'Fewer than 1 in 10'
        return {'ko': f"밤사이 신호가 이만큼 {'나빴던' if down else '좋았던'} 날은 {share_ko} "
                      f"{'하락' if down else '상승'} 출발했어요. ({days}일, {year}년 {month}월부터)",
                'en': f"{share_en} of the days with an overnight signal this {'negative' if down else 'positive'} opened "
                      f"{'lower' if down else 'higher'} ({days} days since {MONTHS_EN[month - 1]} {year})."}
    return {'ko': f"밤사이 신호가 이만큼 {'나빴던' if down else '좋았던'} {days}일 중 {same}일은 "
                  f"{'하락' if down else '상승'} 출발했어요. ({year}년 {month}월부터)",
            'en': f"{same} of {days} days with an overnight signal this {'negative' if down else 'positive'} opened "
                  f"{'lower' if down else 'higher'} (since {MONTHS_EN[month - 1]} {year})."}


def rate_pct(part: Any, whole: Any) -> int | None:
    try:
        return round(100 * int(part) / int(whole)) if int(whole) > 0 else None
    except (TypeError, ValueError):
        return None


def say_before(row: Mapping[str, Any], model: Mapping[str, Any]) -> dict[str, str] | None:
    """H1-H4 (SPEC §4.5); the numbers come from the model file's baseRates."""
    pred, rates = row['predicted'], model.get('baseRates') or {}
    if pred['openCall'] == 'none':
        p = rates.get('weakCloseHitPct')
        if p is None:
            return None
        return {'id': 'H4', 'ko': f'신호가 이 정도로 약한 날은 종가 방향이 {p}%만 맞았어요. 맞히는 척하지 않을게요.',
                'en': f'On days with a signal this weak the close direction was right only {p}% of the time. I will not pretend to call it.'}
    if pred['strength'] == 'strong':
        down = pred['openCall'] == 'down'
        block = rates.get('gapDownIntradayRebound' if down else 'gapUpIntradayExtend') or {}
        p = rate_pct(block.get('rebounded' if down else 'extended'), block.get('days'))
        if p is None:
            return None
        coin_ko = f'과거 {p}%, 반반이에요' if 40 <= p <= 60 else f'과거 {p}%였어요'
        return {'id': 'H1' if down else 'H2',
                'ko': (f'급락 출발 뒤 장중 반등은 {coin_ko}. 장이 열린 뒤 방향은 예보하지 않아요.' if down
                       else f'급등 출발 뒤 더 오른 날은 {coin_ko}. 장이 열린 뒤 방향은 예보하지 않아요.'),
                'en': (f'After a sharp gap down the index rebounded intraday {p}% of the time. I do not call direction once the market opens.' if down
                       else f'After a sharp gap up the index rose further {p}% of the time. I do not call direction once the market opens.')}
    open_hit, close_hit = rates.get('moderateOpenHitPct'), rates.get('moderateCloseHitPct')
    if open_hit is None or close_hit is None:
        return None
    return {'id': 'H3', 'ko': f'이 정도 신호면 시가 방향은 {open_hit}% 맞았어요. 종가까지는 덜 맞아요({close_hit}%).',
            'en': f'At this signal strength the open direction was right {open_hit}% of the time. The close was right {close_hit}% of the time.'}


def say_after(row: Mapping[str, Any], today: date | None = None) -> dict[str, str] | None:
    """H5-H7 once the close is scored (H5 needs the open-to-close move).

    H6 and H7 name the day like the title does: "오늘" for today, the weekday for a later day, the date for an earlier
    one (a Saturday morning showing Friday's result says "11월 6일"). Without `today` the row is taken to be today's.
    """
    pred, actual, result = row['predicted'], row.get('actual') or {}, row.get('result') or {}
    ko_day, en_day = day_words(row['targetSession'], today) if today else ('오늘', 'today')
    open_pct, intraday = actual.get('openPct'), actual.get('intradayPct')
    if pred['openCall'] == 'none' or open_pct is None or result.get('openHit') is None:
        return None
    if result['openHit'] is False:
        return {'id': 'H6',
                'ko': f'{ko_day}은 틀렸어요. 밤사이 신호와 반대로 시가 {fmt_pct(open_pct, 1, True)}로 시작했어요. 성적표에 그대로 남겨요.',
                'en': f"I was wrong {'today' if en_day == 'today' else 'on ' + en_day}. The market opened "
                      f'{fmt_pct(open_pct, 1, True)}, against the overnight signal. The miss stays in the record.'}
    if result.get('openInBand') is True:
        return {'id': 'H7', 'ko': f'{ko_day}은 예보대로였어요. 시가 {fmt_pct(open_pct)}, 예상 범위 안이에요.',
                'en': f"{'Today' if en_day == 'today' else en_day} went as forecast. "
                      f'The open was {fmt_pct(open_pct)}, inside the expected range.'}
    if intraday is None:
        return None
    moved_down = intraday < 0
    further = (pred['openCall'] == 'down') == moved_down
    ko_verb, en_verb = ('빠졌어요', 'down') if moved_down else ('올랐어요', 'up')
    return {'id': 'H5',
            'ko': f"장중에 {fmt_num(abs(intraday))}% {'더 ' if further else ''}{ko_verb}. 장중 움직임은 제가 예보하지 못하는 부분이에요.",
            'en': f"It moved {fmt_num(abs(intraday))}% {'further ' if further else ''}{en_verb} intraday. Intraday moves are the part I do not forecast."}


def range_note_copy(pred: Mapping[str, Any], model: Mapping[str, Any]) -> dict[str, str] | None:
    """How often the shown range held (plan S7). The last year's share when the model has it, because the market got
    rougher than the long history and "8 of 10" would overstate it; the long-history 8 of 10 otherwise."""
    bands = model.get('openBands') or []
    index = {'weak': 0, 'moderate': 1, 'strong': 2}.get(pred.get('strength'), 9)
    band = bands[index] if index < len(bands) else {}
    shown = pred.get('openBand')
    if not band.get('p80AbsErr') or not shown or abs((shown[1] - shown[0]) / 2 - band['p80AbsErr']) > 0.011:
        return None   # a row frozen before the p80 range (or by another model) keeps its range without this note
    cover = band.get('p80RecentCoverage')
    if isinstance(cover, (int, float)) and 0 <= cover <= 1:
        tens = int(cover * 10 + 0.5)
        return {'ko': f'최근 1년 10번 중 {tens}번은 이 범위 안', 'en': f'{tens} of 10 opens fell inside in the last year'}
    return {'ko': '과거 10번 중 8번은 이 범위 안', 'en': '8 of 10 past opens fell inside'}


def range_copy(pred: Mapping[str, Any]) -> dict[str, str] | None:
    band = pred.get('openBand')
    if not band:
        return None
    # Rounded outward to 0.5 steps (review on #59): the range a reader sees always contains the calibrated band,
    # so the coverage note under it never overstates.
    edge_lo, edge_hi = math.floor(min(band) * 2) / 2, math.ceil(max(band) * 2) / 2
    low, high = sorted((edge_lo, edge_hi), key=abs)
    if low == high:
        text = fmt_half(low, True)
        return {'ko': f'예상 범위 {text}% 안팎', 'en': f'Likely around {text}%'}
    return {'ko': f'예상 범위 {fmt_half(low, True)} ~ {fmt_half(high, True)}%',
            'en': f'Likely {fmt_half(low, True)}% to {fmt_half(high, True)}%'}


def size_copy(value: float) -> dict[str, str]:
    if abs(value) < 0.5:
        return {'ko': '보합권', 'en': 'around flat'}
    return {'ko': f'{fmt_half(value, True)}% 안팎', 'en': f'around {fmt_half(value, True)}%'}


def close_copy(row: Mapping[str, Any], model: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """How often a start this size held to the close (plan S7). The close is not presented as a scored forecast:
    `closeCall` and `closeHit` are still recorded, but the card only says what past days like this did."""
    pred, analog = row['predicted'], row.get('analog')
    if pred['openCall'] == 'none' or not analog or int(analog.get('days') or 0) < MIN_ANALOG_DAYS:
        return {}
    up = pred['openCall'] == 'up'
    days, held = int(analog['days']), int(analog['closeSame'])
    rates = model.get('baseRates') or {}
    block = rates.get('gapUpIntradayExtend' if up else 'gapDownIntradayRebound') or {}
    share = rate_pct(block.get('extended' if up else 'rebounded'), block.get('days'))
    # The intraday rate comes from starts of at least the block's gap (1.5%); smaller calls do not get it (review on #59).
    gap = block.get('gapAtLeast' if up else 'gapAtMost')
    if share is not None and not (isinstance(gap, (int, float)) and (pred['openPct'] >= gap if up else pred['openPct'] <= gap)):
        share = None
    if share is None:
        tail_ko, tail_en = f' ({days}일 기준)', f' ({days} days)'
    else:
        coin = '(반반)' if 40 <= share <= 60 else ''
        tail_ko = f" · 장중 {'더 오른' if up else '반등한'} 날 {share}%{coin}"
        tail_en = f" · {'rose further' if up else 'rebounded'} intraday {share}%"
    return {'closeStat': {'ko': f'{days}일 중 {held}일', 'en': f'{held} of {days} days'},
            'closeNote': {'ko': f"이만큼 {'올라' if up else '내려'} 시작해 {'플러스' if up else '마이너스'} 마감" + tail_ko,
                          'en': f"Started like this, closed {'above' if up else 'below'}" + tail_en}}


def open_result_copy(row: Mapping[str, Any]) -> dict[str, str]:
    actual, result, pred = row['actual'], row['result'], row['predicted']
    value = fmt_pct(actual['openPct'], 1, True)
    if pred['openCall'] == 'none':
        return {'ko': f'시가 {value}, 방향 예보 없음', 'en': f'Open {value}: no call made'}
    if result['openHit']:
        return {'ko': f'시가 {value}, 예보 적중', 'en': f'Open {value}: call was right'}
    return {'ko': f'시가 {value}, 예보 빗나감', 'en': f'Open {value}: call was wrong'}


def result_rows_copy(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    pred, actual, result = row['predicted'], row['actual'], row['result']
    open_call = pred['openCall']
    open_pct, close_pct = actual.get('openPct'), actual.get('closePct')
    dash = {'ko': '—', 'en': '—'}

    def actual_pct(value: float | None) -> dict[str, str]:
        return dash if value is None else {'ko': fmt_pct(value, 1, True), 'en': fmt_pct(value, 1, True)}

    return [
        {'key': 'open', 'label': {'ko': '시가 방향', 'en': 'Open direction'},
         'forecast': {'ko': DIRECTION_KO[open_call], 'en': DIRECTION_EN[open_call].capitalize()},
         'actual': actual_pct(open_pct), 'hit': result.get('openHit')},
        {'key': 'open_size', 'label': {'ko': '시가 크기', 'en': 'Open size'},
         'forecast': size_copy(pred['openPct']) if open_call != 'none' else dash,
         'actual': actual_pct(open_pct), 'hit': result.get('openInBand')},
        {'key': 'close', 'label': {'ko': '종가 방향', 'en': 'Close direction'},
         'forecast': {'ko': '예보 안 함', 'en': 'Not forecast'},
         'actual': actual_pct(close_pct), 'hit': None},   # plan S7: closeHit stays in `result`, the card does not score it
    ]


def result_headline_copy(row: Mapping[str, Any], today: date) -> tuple[dict[str, str], dict[str, str]] | None:
    """Headline and badge for a fully scored day (not shown while only the open is scored)."""
    pred, result = row['predicted'], row.get('result') or {}
    ko_day, en_day = day_words(row['targetSession'], today)
    is_today = en_day == 'today'
    if pred['openCall'] == 'none':
        return ({'ko': f'{ko_day}은 방향을 내지 않았어요',
                 'en': 'No call today' if is_today else f'No call for {en_day}'},
                {'ko': '방향 없음', 'en': 'No call', 'tone': 'neutral'})
    if result.get('openHit') is None:
        return None
    if result['openHit'] is False:
        return ({'ko': f'{ko_day} 예보는 빗나갔어요',
                 'en': "Today's call was wrong" if is_today else f"{en_day}'s call was wrong"},
                {'ko': '빗나감', 'en': 'Missed', 'tone': 'minus'})
    badge = {'ko': '방향 적중', 'en': 'Direction right', 'tone': 'plus'}
    if result.get('openInBand') is True:
        return ({'ko': f'{ko_day}은 예보대로였어요',
                 'en': 'Today went as forecast' if is_today else f'{en_day} went as forecast'}, badge)
    return ({'ko': '방향은 맞았고, 크기는 빗나갔어요', 'en': 'Direction was right, size was off'}, badge)


def build_copy(row: Mapping[str, Any], today: date, model: Mapping[str, Any]) -> dict[str, Any]:
    """The card text for the current row. Every number comes from the row or the model file."""
    copy: dict[str, Any] = {'title': title_copy(row, today), 'badge': badge_copy(row)}
    if row['state'] == 'no_forecast' or row.get('paused') or not row.get('predicted'):
        return copy
    pred = row['predicted']
    if isinstance(row.get('inputs'), dict):
        copy['chips'] = chips_copy(row['inputs'])
    if pred['openCall'] != 'none':
        copy['openStat'] = size_copy(pred['openPct'])
        band = range_copy(pred)
        if band:
            copy['openRange'] = band
            note = range_note_copy(pred, model)
            if note:
                copy['openRangeNote'] = note
        copy.update(close_copy(row, model))
        past = past_copy(row)
        if past:
            copy['past'] = past
    say = say_before(row, model)
    if row['state'] in SCORED_STATES and row.get('actual'):
        copy['openResult'] = open_result_copy(row)
    if row['state'] == 'closed_scored' and row.get('actual'):
        after = say_after(row, today)
        if after:
            say = after
        ko_day, en_day = day_words(row['targetSession'], today)
        copy['resultTitle'] = {'ko': f'{ko_day} 예보 결과', 'en': 'Forecast result' if en_day == 'today' else f'{en_day} forecast result'}
        copy['resultRows'] = result_rows_copy(row)
        headline = result_headline_copy(row, today)
        if headline:
            copy['resultHeadline'], copy['resultBadge'] = headline
    if say:
        copy['say'] = say
    return copy


# ---------------------------------------------------------------- the block

def kospi_series_from(prices: Mapping[str, Mapping[str, Any]], series: Mapping[str, v3.Series]) -> v3.Series:
    """KOSPI closes for `forecast_inputs`: Naver's official rows when we have them (as in training), else Yahoo's."""
    closes = [(day, row.get('close')) for day, row in prices.items() if row.get('close')]
    if closes:
        return v3.Series(closes)
    return series.get('^KS11') or v3.Series([])


def public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Ledger line: the current-row shape without `copy` and `inputs` (SPEC §6.4)."""
    return {k: deepcopy(v) for k, v in row.items() if k not in ('copy', 'inputs')}


def ordered_row(row: Mapping[str, Any]) -> dict[str, Any]:
    head = {key: deepcopy(row[key]) for key in ROW_KEYS if key in row}
    head.update({k: deepcopy(v) for k, v in row.items() if k not in head})
    return head


def compute_block(previous_public: Mapping[str, Any] | None, series: Mapping[str, v3.Series],
                  prices: Mapping[str, Mapping[str, Any]], now: datetime, model: Mapping[str, Any]) -> dict[str, Any]:
    """The whole morning-forecast block for `now`. Pure: all data comes in as arguments."""
    now = as_aware(now)
    today = now.astimezone(KST).date()
    target = target_session_for(now)
    t_next = target.isoformat()
    rows, carried, _ = load_previous(previous_public)
    us_series = {symbol: series[symbol] for _, symbol in US_INPUT_SYMBOLS if series.get(symbol) is not None}
    kospi = kospi_series_from(prices, series)
    have_us = len(us_series) == len(US_INPUT_SYMBOLS)
    if not prices and not have_us:
        raise ForecastUnavailable('market_data_unavailable')  # nothing to compute or score with: keep the old block as is
    if not rows and not (have_us and prices):
        raise ForecastUnavailable('market_data_unavailable')  # never write a first ledger out of a data outage
    frozen_before = {key: frozen_view(row) for key, row in rows.items() if row['state'] not in ('pending', 'no_forecast')}

    if not rows:  # first deployment: recompute the last 20 sessions from known data (SPEC §6.3 "처음 채우기")
        days = trading_days_back(target, BACKFILL_SESSIONS + 1)  # +1: the oldest day needs its own basis day
        for day in reversed(days[:-1]):
            rows[day.isoformat()] = row_from_data(day.isoformat(), kr_day_before(day).isoformat(), us_series, kospi,
                                                  model, 'backfill', None)
    else:  # sessions the run missed entirely: record them as they were, never recompute them as if issued
        floor = (trading_days_back(target, LEDGER_SESSIONS) or [target])[-1]
        day = max(kr_day_after(date.fromisoformat(max(rows))), floor)
        while day < target:
            key = day.isoformat()
            if key not in rows:
                basis = kr_day_before(day).isoformat()
                sessions = us_calendar_sessions(basis, key)
                rows[key] = make_no_forecast(new_row(key, basis, 'live'),
                                             'missed_window' if sessions else 'us_market_closed', sessions)
            day = kr_day_after(day)

    existing = rows.get(t_next)
    if existing is None or existing['state'] == 'pending':
        decision = issue_decision(target, now, us_series, kospi, model)
        row = new_row(t_next, decision['basis'], 'live')
        if decision['action'] == 'no_forecast':
            rows[t_next] = make_no_forecast(row, decision['reason'], decision['sessions'])
        elif decision['action'] == 'issue':
            rows[t_next] = make_issued(row, decision['fi'], model, iso_utc(now), paused=is_paused(rows))

    for row in rows.values():
        if row['state'] in ('issued', 'open_scored'):
            score_row(row, prices, now)
            settle_unscorable(row, today)
        elif row['state'] == 'no_forecast':
            fill_actual_only(row, prices, now)

    for key, before in frozen_before.items():  # the freeze rule, enforced in code as well as in the contract
        if key in rows and frozen_view(rows[key]) != before:
            raise ForecastUnavailable('frozen_row_changed')
    carried = trim_ledger(rows, carried)
    scorecard = build_scorecard(rows, carried, model)
    live_rows = [r for r in rows.values() if r.get('origin') == 'live' and r['state'] != 'pending']
    current_row = max(live_rows, key=lambda r: r['targetSession']) if live_rows else None
    current = None
    if current_row is not None:
        current = ordered_row({**current_row, 'copy': build_copy(current_row, today, model)})
    return {
        'schemaVersion': SCHEMA_VERSION,
        'modelVersion': model.get('modelVersion'),
        'generatedAt': iso_utc(now),
        'status': 'paused' if current_row is not None and current_row.get('paused') else 'ok',
        'phase': phase_for(current_row, now),
        'current': current,
        'ledger': [ordered_row(public_row(rows[key])) for key in sorted(rows)],
        'scorecard': scorecard,
        'nextIssueAt': next_issue_at(rows, target),
        'disclaimer': DISCLAIMER,
    }


def unavailable_block(previous_public: Mapping[str, Any] | None, now: datetime, error: BaseException) -> dict[str, Any]:
    """Never lose the ledger: the previous block's ledger, current row and scorecard ride along unchanged."""
    reason = str(error) if isinstance(error, ForecastUnavailable) else type(error).__name__
    _, _, previous = load_previous(previous_public)
    block = deepcopy(previous) if previous else {'current': None, 'ledger': [], 'scorecard': None, 'nextIssueAt': None}
    block.update({'schemaVersion': SCHEMA_VERSION, 'modelVersion': MODEL_VERSION, 'generatedAt': iso_utc(as_aware(now)),
                  'status': 'unavailable', 'phase': 'off', 'reason': reason, 'disclaimer': DISCLAIMER})
    return block


def apply_morning_forecast(snapshot: dict[str, Any], previous_public: Mapping[str, Any] | None,
                           series: Mapping[str, v3.Series] | None, now: datetime | None = None,
                           fetch: Fetch | None = None) -> dict[str, Any]:
    """Attach `morningForecast` to the snapshot. Never raises; on failure the block says why and keeps the old ledger."""
    now = as_aware(now) if now else datetime.now(timezone.utc)
    try:
        import polarmeter_v3_inputs as inputs  # imported here: that module imports this one at load time
        model = load_model()
        if model.get('modelVersion') != MODEL_VERSION:
            raise ForecastUnavailable('model_version_mismatch')
        prices = inputs.fetch_kospi_daily(fetch or inputs.fetch_json)
        block = compute_block(previous_public, series or {}, prices, now, model)
        assert_public_morning_forecast(block)  # a malformed block must not reach the publish checks
    except Exception as error:  # the forecast layer must never stop a publish
        try:
            block = unavailable_block(previous_public, now, error)
        except Exception:  # pragma: no cover - last resort
            block = {'schemaVersion': SCHEMA_VERSION, 'modelVersion': MODEL_VERSION, 'generatedAt': iso_utc(now),
                     'status': 'unavailable', 'phase': 'off', 'reason': 'ForecastError', 'current': None,
                     'ledger': [], 'scorecard': None, 'nextIssueAt': None, 'disclaimer': DISCLAIMER}
    snapshot['morningForecast'] = block
    return snapshot


# ---------------------------------------------------------------- public-shape check (security / pages contracts)

def assert_public_forecast_if_present(snapshot: Mapping[str, Any], manifest: Mapping[str, Any] | None = None) -> None:
    """Pre-publish check that never blocks a publish for lack of the block (SPEC §5 decision 5).

    Old public data (the rollback path, or the first deployment) has no `morningForecast`: that passes. When the block
    is there it must be well formed and the manifest's status line must match it.
    """
    block = snapshot.get('morningForecast')
    if block is None:
        return
    assert_public_morning_forecast(block)
    if manifest is not None and 'morningForecastStatus' in manifest and manifest['morningForecastStatus'] != block['status']:
        raise AssertionError('manifest morningForecastStatus must match the public morningForecast status')


def assert_public_morning_forecast(block: Any) -> None:
    """Structural check of the public block; raises AssertionError. Used by the contracts and the pages checks."""
    assert isinstance(block, dict), 'morningForecast must be an object'
    assert block.get('schemaVersion') == SCHEMA_VERSION, 'morningForecast schemaVersion'
    assert block.get('status') in STATUSES, f"morningForecast status {block.get('status')!r}"
    assert block.get('phase') in PHASES, f"morningForecast phase {block.get('phase')!r}"
    assert isinstance(block.get('generatedAt'), str) and block['generatedAt'].endswith('Z'), 'morningForecast generatedAt'
    ledger = block.get('ledger')
    assert isinstance(ledger, list) and len(ledger) <= LEDGER_SESSIONS, 'morningForecast ledger'
    for row in ledger:
        assert valid_row(row), f'morningForecast ledger row {row!r}'
        assert 'copy' not in row and 'inputs' not in row, 'ledger rows carry no copy or inputs'
        if row['state'] == 'no_forecast':
            assert row.get('noForecastReason') in NO_FORECAST_REASONS, 'noForecastReason'
    if block['status'] == 'unavailable':
        assert isinstance(block.get('reason'), str) and block['reason'], 'unavailable block needs a reason'
        return
    current = block.get('current')
    if current is not None:
        assert valid_row(current) and isinstance(current.get('copy'), dict), 'morningForecast current'
        assert isinstance(current['copy'].get('title'), dict) and current['copy']['title'].get('ko'), 'copy.title'
    score = block.get('scorecard')
    assert isinstance(score, dict) and isinstance(score.get('recent'), dict), 'morningForecast scorecard'
    marks = score['recent'].get('marks')
    assert isinstance(marks, list) and len(marks) <= RECENT_SESSIONS and all(m in ('hit', 'miss', 'none') for m in marks), 'marks'
    assert isinstance(block.get('disclaimer'), dict) and block['disclaimer'].get('ko'), 'disclaimer'
    json.dumps(block, ensure_ascii=False, allow_nan=False)
