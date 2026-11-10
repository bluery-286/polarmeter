#!/usr/bin/env python3
"""Evening brief (꽁치의 저녁 브리핑): tonight's US session start and how often such a start held to the close.

What it says, and what it does not (research out/us-forecast-research-20261109, 2nd opinion 마스 pm-us-res-01):
- At 21:00 KST the S&P futures (ES=F) move since the previous US close is already known to anyone with a broker
  app. The brief does not "forecast" the open: it shows that start, and how often a start of that size *held* to the
  close (the S&P 500 closed on the same side of the previous close): about 3 in 4 in 2024-06 ~ 2026-11.
- The move after the open is about a coin flip (~55%). The copy says so every time: this is not a chase signal.
- No call on FOMC decision days (the 02:00/03:00 KST decision overturns the start: 7 of 19 held) and in futures-roll
  weeks (the continuous ES=F contract jumps on the roll and fakes a start).
- A close within ±0.2% of the previous close is "flat", counted apart from held/flipped.

Pure functions plus `apply_evening_brief(snapshot, previous_public, now, fetch)`, which never raises.
The app shows this block only when its own feature switch is on; the server records it from day one.
"""
from __future__ import annotations

import math
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import json
import urllib.parse

import polarmeter_market_calendar as cal

SCHEMA_VERSION = 'polarmeter-evening-brief-v1'
MODEL_VERSION = 'us-start-hold-v1'
MODEL_PATH = Path(__file__).with_name('polarmeter_evening_model.json')
KST = cal.KST_ZONE
NY = cal.NY_ZONE

FEATURE_UTC_HOUR = 12           # 21:00 KST: the research measuring time
ISSUE_FROM_KST = time(21, 0)
US_OPEN_ET = time(9, 30)
SCORE_AFTER_CLOSE_MINUTES = 15
CALL_MIN_ABS_PCT = 0.3          # smaller starts are "no clear direction"
FLAT_ABS_PCT = 0.2              # a close this near the previous close is "flat"
BANDS = ((0.3, 0.5), (0.5, 0.8), (0.8, None))
LEDGER_SESSIONS = 60
BACKFILL_SESSIONS = 20
RECENT_CALLS = 20
VOID_AFTER_DAYS = 4
MAX_BAR_AGE_HOURS = 3.0         # the futures price must be this fresh at issue time
MAX_BASE_GAP_HOURS = 2.0        # the base bar must end this near 16:00 New York
SUSPECT_ABS_PCT = 8.0

STATES = ('pending', 'issued', 'scored', 'rest', 'void')
CALLS = ('up', 'down', 'none')
OUTCOMES = ('held', 'flat', 'flipped')
REST_REASONS = ('fomc', 'futures_roll', 'missed_window', 'data_unavailable', 'inputs_suspect')
PHASES = ('evening', 'us_open', 'scored', 'off')
ROW_KEYS = ('targetSession', 'prevSession', 'origin', 'state', 'issuedAt', 'restReason', 'call',
            'inputs', 'stats', 'actual', 'outcome')
DISCLAIMER = {'ko': '과거 기록이며 앞으로의 결과를 보장하지 않습니다. 매매 권유가 아닙니다.',
              'en': 'Past record only; it does not promise future results. Not a trading recommendation.'}

YAHOO_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}'
FUTURES = (('esPct', 'ES=F'), ('nqPct', 'NQ=F'))
INDEX_SYMBOL = '^GSPC'

# FOMC decision days (New York dates). History for the stats table and the schedule ahead; the snapshot's
# SCHEDULED_MACRO_EVENTS 'fomc_rate' list is checked against this one in the contract.
FOMC_DECISION_DATES = frozenset(date.fromisoformat(d) for d in (
    '2024-01-31', '2024-03-20', '2024-05-01', '2024-06-12', '2024-07-31', '2024-09-18', '2024-11-07', '2024-12-18',
    '2025-01-29', '2025-03-19', '2025-05-07', '2025-06-18', '2025-07-30', '2025-09-17', '2025-10-29', '2025-12-10',
    '2026-01-28', '2026-03-18', '2026-04-29', '2026-06-17', '2026-07-29', '2026-09-16', '2026-10-28', '2026-12-09',
    '2027-01-27', '2027-03-17', '2027-04-28', '2027-06-09', '2027-07-28', '2027-09-15', '2027-10-27', '2027-12-08'))

Fetch = Callable[[str], Any]


class BriefUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------- calendar

def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def as_aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def us_open_day(day: date) -> bool:
    return cal.us_market_closed_reason(datetime.combine(day, time(12), NY)) is None


def us_day_after(day: date) -> date:
    day += timedelta(days=1)
    while not us_open_day(day):
        day += timedelta(days=1)
    return day


def us_day_before(day: date) -> date:
    day -= timedelta(days=1)
    while not us_open_day(day):
        day -= timedelta(days=1)
    return day


def us_days_back(day: date, count: int) -> list[date]:
    """`day` and the `count - 1` US sessions before it, newest first."""
    out = [day]
    while len(out) < count:
        out.append(us_day_before(out[-1]))
    return out


def close_at(day: date) -> datetime:
    when = datetime.combine(day, time(12), NY)
    return datetime.combine(day, cal.us_equity_close_time(when), NY)


def open_at(day: date) -> datetime:
    return datetime.combine(day, US_OPEN_ET, NY)


def issue_from(day: date) -> datetime:
    return datetime.combine(day, ISSUE_FROM_KST, KST)


def target_session_for(now: datetime) -> date:
    """The US session the brief is about: today's (New York) until its close is scored, then the next one."""
    day = now.astimezone(NY).date()
    if us_open_day(day) and now < close_at(day) + timedelta(minutes=SCORE_AFTER_CLOSE_MINUTES):
        return day
    return us_day_after(day)


def third_friday(year: int, month: int) -> date:
    day = date(year, month, 15)
    return day + timedelta(days=(4 - day.weekday()) % 7)


def in_roll_window(day: date) -> bool:
    """ES quarterly roll: from 12 days before the third Friday of Mar/Jun/Sep/Dec through that Friday."""
    if day.month not in (3, 6, 9, 12):
        return False
    friday = third_friday(day.year, day.month)
    return friday - timedelta(days=12) <= day <= friday


def rest_reason(day: date) -> str | None:
    if day in FOMC_DECISION_DATES:
        return 'fomc'
    if in_roll_window(day):
        return 'futures_roll'
    return None


# ---------------------------------------------------------------- data

def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def hourly_bars(payload: Mapping[str, Any]) -> list[tuple[int, float]]:
    """(bar start epoch seconds, close) for a Yahoo 60m chart, oldest first, null closes dropped."""
    result = ((payload.get('chart') or {}).get('result') or [None])[0] or {}
    stamps = result.get('timestamp') or []
    closes = (((result.get('indicators') or {}).get('quote') or [{}])[0]).get('close') or []
    return sorted((int(t), c) for t, c in ((t, _num(c)) for t, c in zip(stamps, closes)) if c is not None)


def daily_open_close(payload: Mapping[str, Any]) -> dict[str, tuple[float | None, float | None]]:
    """New York date -> (open, close) from a Yahoo 1d chart. The live bar is included; callers decide if it settled."""
    result = ((payload.get('chart') or {}).get('result') or [None])[0] or {}
    stamps = result.get('timestamp') or []
    quote = (((result.get('indicators') or {}).get('quote') or [{}])[0])
    out = {}
    for t, o, c in zip(stamps, quote.get('open') or [], quote.get('close') or []):
        out[datetime.fromtimestamp(int(t), NY).date().isoformat()] = (_num(o), _num(c))
    return out


def price_at(bars: list[tuple[int, float]], moment: datetime, max_age_hours: float) -> float | None:
    """Close of the last bar that ended by `moment`, if it ended within `max_age_hours`."""
    ts = moment.timestamp()
    best = None
    for start, close in bars:
        if start + 3600 <= ts:
            best = (start + 3600, close)
        else:
            break
    if best is None or ts - best[0] > max_age_hours * 3600:
        return None
    return best[1]


def futures_move(bars: list[tuple[int, float]], prev_session: str, at: datetime) -> float | None:
    """% move from the previous session's close (16:00 New York, 13:00 on early closes) to `at`."""
    base = price_at(bars, close_at(date.fromisoformat(prev_session)), MAX_BASE_GAP_HOURS)
    now = price_at(bars, at, MAX_BAR_AGE_HOURS)
    if not base or now is None:
        return None
    return (now / base - 1) * 100


def fetch_inputs(fetch: Fetch) -> dict[str, Any]:
    def chart(symbol: str, range_: str, interval: str) -> Mapping[str, Any]:
        return fetch(YAHOO_URL.format(symbol=urllib.parse.quote(symbol, safe=''), range=range_, interval=interval))
    data: dict[str, Any] = {'futures': {}, 'index': {}}
    for key, symbol in FUTURES:
        try:
            data['futures'][key] = hourly_bars(chart(symbol, '1mo', '60m'))
        except Exception:  # one missing future must not stop the other
            data['futures'][key] = []
    data['index'] = daily_open_close(chart(INDEX_SYMBOL, '3mo', '1d'))
    return data


# ---------------------------------------------------------------- model (stats table)

def load_model(path: Path | str = MODEL_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def outcome_of(call: str, close_pct: float) -> str:
    if abs(close_pct) < FLAT_ABS_PCT:
        return 'flat'
    return 'held' if (close_pct > 0) == (call == 'up') else 'flipped'


def band_of(abs_pct: float) -> str | None:
    for low, high in BANDS:
        if abs_pct >= low and (high is None or abs_pct < high):
            return f'{low:g}-{high:g}' if high is not None else f'{low:g}+'
    return None


def stats_table(rows: Iterable[tuple[float, float, float]]) -> dict[str, Any]:
    """rows: (futures %, close vs previous close %, close vs open %) for days outside rest days."""
    bands: dict[str, dict[str, int]] = {}
    total = {'days': 0, 'held': 0, 'flat': 0, 'flipped': 0}
    after_open_same = after_open_days = 0
    for es_pct, close_pct, after_open_pct in rows:
        band = band_of(abs(es_pct))
        if band is None:
            continue
        call = 'up' if es_pct > 0 else 'down'
        outcome = outcome_of(call, close_pct)
        cell = bands.setdefault(band, {'days': 0, 'held': 0, 'flat': 0, 'flipped': 0})
        for bucket in (cell, total):
            bucket['days'] += 1
            bucket[outcome] += 1
        after_open_days += 1
        after_open_same += (after_open_pct > 0) == (call == 'up') and after_open_pct != 0
    return {'modelVersion': MODEL_VERSION, 'callMinAbsPct': CALL_MIN_ABS_PCT, 'flatAbsPct': FLAT_ABS_PCT,
            'bands': {key: bands[key] for key in sorted(bands, key=lambda k: float(k.split('-')[0].rstrip('+')))},
            'total': total,
            'afterOpenSameShare': round(after_open_same / after_open_days, 3) if after_open_days else None}


# ---------------------------------------------------------------- rows

def new_row(target: str, prev: str, origin: str) -> dict[str, Any]:
    return {'targetSession': target, 'prevSession': prev, 'origin': origin, 'state': 'pending', 'issuedAt': None,
            'restReason': None, 'call': None, 'inputs': None, 'stats': None, 'actual': None, 'outcome': None}


def make_issued(row: dict[str, Any], inputs: Mapping[str, float | None], model: Mapping[str, Any],
                issued_at: str | None) -> dict[str, Any]:
    es_pct = inputs['esPct']
    call = 'none' if abs(es_pct) < CALL_MIN_ABS_PCT else ('up' if es_pct > 0 else 'down')
    stats = None
    if call != 'none':
        band = band_of(abs(es_pct))
        cell = (model.get('bands') or {}).get(band)
        if cell:
            stats = {'band': band, **{k: int(cell[k]) for k in ('days', 'held', 'flat', 'flipped')},
                     'afterOpenSameShare': model.get('afterOpenSameShare'), 'period': model.get('period')}
    row.update({'state': 'issued', 'issuedAt': issued_at, 'call': call,
                'inputs': {k: (round(v, 2) if v is not None else None) for k, v in inputs.items()}, 'stats': stats})
    return row


def make_rest(row: dict[str, Any], reason: str) -> dict[str, Any]:
    row.update({'state': 'rest', 'restReason': reason})
    return row


def inputs_at(data: Mapping[str, Any], prev: str, at: datetime) -> dict[str, float | None]:
    futures = data.get('futures') or {}
    return {key: futures_move(futures.get(key) or [], prev, at) for key, _ in FUTURES}


def decide(row: dict[str, Any], data: Mapping[str, Any], at: datetime, model: Mapping[str, Any],
           issued_at: str | None) -> dict[str, Any]:
    reason = rest_reason(date.fromisoformat(row['targetSession']))
    if reason:
        return make_rest(row, reason)
    inputs = inputs_at(data, row['prevSession'], at)
    if inputs.get('esPct') is None:
        return make_rest(row, 'data_unavailable')
    if any(v is not None and abs(v) > SUSPECT_ABS_PCT for v in inputs.values()):
        return make_rest(row, 'inputs_suspect')
    return make_issued(row, inputs, model, issued_at)


def score_row(row: dict[str, Any], index: Mapping[str, tuple[float | None, float | None]], now: datetime) -> None:
    target = date.fromisoformat(row['targetSession'])
    if now < close_at(target) + timedelta(minutes=SCORE_AFTER_CLOSE_MINUTES):
        return
    today, prev = index.get(row['targetSession']), index.get(row['prevSession'])
    if not today or not prev or not today[1] or not prev[1]:
        if (now.astimezone(NY).date() - target).days >= VOID_AFTER_DAYS and row['state'] == 'issued':
            row['state'] = 'void'
        return
    close_pct = (today[1] / prev[1] - 1) * 100
    after_open = (today[1] / today[0] - 1) * 100 if today[0] else None
    row['actual'] = {'closePct': round(close_pct, 2),
                     'afterOpenPct': round(after_open, 2) if after_open is not None else None, 'source': 'yahoo_gspc'}
    if row['state'] == 'issued':
        row['state'] = 'scored'
        row['outcome'] = outcome_of(row['call'], close_pct) if row['call'] in ('up', 'down') else None


def frozen_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: deepcopy(row.get(k)) for k in ('targetSession', 'prevSession', 'origin', 'issuedAt', 'restReason',
                                               'call', 'inputs', 'stats')}


def valid_row(row: Any) -> bool:
    return isinstance(row, dict) and isinstance(row.get('targetSession'), str) and row.get('state') in STATES


def load_previous(previous_public: Mapping[str, Any] | None) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    block = (previous_public or {}).get('eveningBrief') or {}
    if block.get('schemaVersion') != SCHEMA_VERSION:
        return {}, {}
    rows = {r['targetSession']: {k: deepcopy(r.get(k)) for k in ROW_KEYS} for r in block.get('ledger') or []
            if valid_row(r)}
    carried = {k: int(v) for k, v in ((block.get('scorecard') or {}).get('carried') or {}).items()
               if k in ('calls',) + OUTCOMES and isinstance(v, int)}
    return rows, carried


def trim_ledger(rows: dict[str, dict[str, Any]], carried: dict[str, int]) -> dict[str, int]:
    carried = {k: carried.get(k, 0) for k in ('calls',) + OUTCOMES}
    for key in sorted(rows)[:-LEDGER_SESSIONS]:
        row = rows.pop(key)
        if row['state'] == 'scored' and row.get('outcome') and row.get('origin') == 'live':
            carried['calls'] += 1
            carried[row['outcome']] += 1
    return carried


def build_scorecard(rows: Mapping[str, Mapping[str, Any]], carried: Mapping[str, int]) -> dict[str, Any]:
    scored = [rows[k] for k in sorted(rows) if rows[k]['state'] == 'scored' and rows[k].get('outcome')]
    recent = scored[-RECENT_CALLS:]
    count = lambda items, outcome: sum(1 for r in items if r['outcome'] == outcome)
    live = [r for r in scored if r.get('origin') == 'live']
    return {
        'recent': {'calls': len(recent), **{o: count(recent, o) for o in OUTCOMES},
                   'liveCalls': sum(1 for r in recent if r.get('origin') == 'live')},
        'liveTotal': {'calls': len(live) + carried.get('calls', 0),
                      **{o: count(live, o) + carried.get(o, 0) for o in OUTCOMES}},
        'carried': dict(carried),
    }


def phase_for(row: Mapping[str, Any] | None, now: datetime) -> str:
    if not row:
        return 'off'
    target = date.fromisoformat(row['targetSession'])
    if row['state'] in ('scored', 'void') or now >= close_at(target):
        return 'scored'
    if now >= open_at(target):
        return 'us_open'
    return 'evening' if row['state'] in ('issued', 'rest') else 'off'


# ---------------------------------------------------------------- copy

MINUS = '−'


def fmt_pct(value: float, decimals: int = 2) -> str:
    text = f'{abs(value):.{decimals}f}%'
    return ('+' if value > 0 else MINUS if value < 0 else '') + text


def events_tonight(events: Iterable[Mapping[str, Any]], row: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Scheduled US releases between now and the session's close, with their KST time."""
    target = date.fromisoformat(row['targetSession'])
    start, end = min(now, open_at(target)) - timedelta(hours=1), close_at(target) + timedelta(hours=12)
    out = []
    for item in events:
        at = _parse(item.get('at'))
        if at is None or not (start <= at <= end):
            continue
        out.append({'id': item.get('id'), 'at': iso_utc(at), 'kst': at.astimezone(KST).strftime('%H:%M'),
                    'label': {'ko': item.get('labelKo') or '', 'en': item.get('labelEn') or item.get('labelKo') or ''}})
    return sorted(out, key=lambda e: e['at'])


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return as_aware(datetime.fromisoformat(value.replace('Z', '+00:00')))
    except ValueError:
        return None


REST_COPY = {
    'fomc': ('오늘 밤은 FOMC 금리 결정 날이라 쉬어요', 'FOMC decision night: no read tonight',
             '새벽 금리 발표가 출발 흐름을 자주 뒤집어서, 이런 날은 방향을 말하지 않아요.',
             'The rate decision often overturns the start, so we skip these nights.'),
    'futures_roll': ('선물 만기 교체 주간이라 쉬어요', 'Futures roll week: no read tonight',
                     '이 주간엔 선물 가격이 바뀌는 월물 때문에 튀어서 출발 신호가 정확하지 않아요.',
                     'Contract roll distorts the futures price this week.'),
    'missed_window': ('오늘 밤 브리핑을 제때 만들지 못했어요', 'Tonight\'s brief was not ready in time', '', ''),
    'data_unavailable': ('선물 시세를 받지 못해 쉬어요', 'Futures data unavailable tonight', '', ''),
    'inputs_suspect': ('선물 시세가 이상해서 쉬어요', 'Futures data looks off tonight', '', ''),
}


def build_copy(row: Mapping[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    copy: dict[str, Any] = {'events': events}
    if row['state'] == 'rest':
        ko, en, why_ko, why_en = REST_COPY.get(row.get('restReason') or '', REST_COPY['data_unavailable'])
        copy['title'] = {'ko': ko, 'en': en}
        copy['note'] = {'ko': why_ko, 'en': why_en} if why_ko else None
    elif row.get('inputs'):
        inputs, call = row['inputs'], row.get('call')
        es_pct, nq_pct = inputs.get('esPct'), inputs.get('nqPct')
        copy['title'] = {
            'up': {'ko': '오늘 밤 미국장, 강세 출발 구간이에요', 'en': 'US tonight: starting strong'},
            'down': {'ko': '오늘 밤 미국장, 약세 출발 구간이에요', 'en': 'US tonight: starting weak'},
        }.get(call, {'ko': '오늘 밤 미국장은 출발 방향이 뚜렷하지 않아요', 'en': 'US tonight: no clear start'})
        parts_ko, parts_en = [f'S&P 선물 {fmt_pct(es_pct)}'], [f'S&P futures {fmt_pct(es_pct)}']
        if nq_pct is not None:
            parts_ko.append(f'나스닥 선물 {fmt_pct(nq_pct)}')
            parts_en.append(f'Nasdaq futures {fmt_pct(nq_pct)}')
        issued = _parse(row.get('issuedAt'))
        at_ko = issued.astimezone(KST).strftime('%H:%M') if issued else '21:00'
        copy['start'] = {'ko': ' · '.join(parts_ko) + f' ({at_ko} 기준, 전날 마감 대비)',
                         'en': ' · '.join(parts_en) + f' ({at_ko} KST, vs previous close)'}
        stats = row.get('stats')
        if stats and call in ('up', 'down'):
            side_ko, side_en = ('플러스', 'above') if call == 'up' else ('마이너스', 'below')
            copy['hold'] = {
                # plan S7: the lead first, then how often a lead like it was still there at the close
                'ko': f"선물이 이만큼 {'앞선' if call == 'up' else '밀린'} 날 {stats['days']}번 중 {stats['held']}번은"
                      f" 그 {side_ko}가 마감까지 남았어요 (보합 {stats['flat']}번).",
                'en': f"Of {stats['days']} days with futures this far {'ahead' if call == 'up' else 'behind'},"
                      f" {stats['held']} still closed {side_en} the previous close ({stats['flat']} flat)."}
            share = stats.get('afterOpenSameShare')
            more_ko = '더 오를지는' if call == 'up' else '더 내릴지는'
            copy['caveat'] = {
                'ko': f'다만 장이 열린 뒤 {more_ko} 반반이에요' + (f'({round(share * 100)}%)' if share else '')
                      + '. 따라 사거나 팔라는 신호가 아니에요.',
                'en': 'After the open it is about a coin flip' + (f' ({round(share * 100)}%)' if share else '')
                      + '. Not a signal to chase.'}
        copy['kospi'] = {'ko': '밤사이 반도체와 한국 관련 ETF 흐름은 내일 아침 8:05 꽁치 예보에 이어서 반영돼요.',
                         'en': 'Overnight chips and Korea ETF moves feed tomorrow\'s 8:05 morning forecast.'}
    if row['state'] == 'scored' and row.get('actual'):
        close_pct = row['actual']['closePct']
        verdict = {'held': ('출발 방향을 지켰어요', 'start held'), 'flat': ('보합 마감', 'flat close'),
                   'flipped': ('출발과 반대로 끝났어요', 'start flipped')}.get(row.get('outcome') or '')
        tail_ko = f' · {verdict[0]}' if verdict else ''
        tail_en = f' · {verdict[1]}' if verdict else ''
        copy['result'] = {'ko': f'S&P 500 {fmt_pct(close_pct)} 마감{tail_ko}',
                          'en': f'S&P 500 closed {fmt_pct(close_pct)}{tail_en}'}
    return copy


# ---------------------------------------------------------------- block

def ordered_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: deepcopy(row.get(k)) for k in ROW_KEYS}
    if 'copy' in row:
        out['copy'] = deepcopy(row['copy'])
    return out


def compute_block(previous_public: Mapping[str, Any] | None, data: Mapping[str, Any], now: datetime,
                  model: Mapping[str, Any], events: list[Mapping[str, Any]]) -> dict[str, Any]:
    now = as_aware(now)
    target = target_session_for(now)
    rows, carried = load_previous(previous_public)
    have_futures = bool((data.get('futures') or {}).get('esPct'))
    index = data.get('index') or {}
    if not rows and not (have_futures and index):
        raise BriefUnavailable('market_data_unavailable')
    frozen_before = {k: frozen_view(r) for k, r in rows.items() if r['state'] != 'pending'}

    if not rows:  # first deployment: the last 20 sessions from the hourly history, marked as backfill
        for day in reversed(us_days_back(us_day_before(target), BACKFILL_SESSIONS)):
            key = day.isoformat()
            at = datetime.combine(day, time(FEATURE_UTC_HOUR), timezone.utc)
            rows[key] = decide(new_row(key, us_day_before(day).isoformat(), 'backfill'), data, at, model, None)
    else:  # sessions the runs missed entirely
        day = us_day_after(date.fromisoformat(max(rows)))
        while day < target:
            key = day.isoformat()
            rows.setdefault(key, make_rest(new_row(key, us_day_before(day).isoformat(), 'live'), 'missed_window'))
            day = us_day_after(day)

    key = target.isoformat()
    existing = rows.get(key)
    if (existing is None or existing['state'] == 'pending') and now >= issue_from(target):
        row = new_row(key, us_day_before(target).isoformat(), 'live')
        if now >= open_at(target):
            rows[key] = make_rest(row, 'missed_window')
        else:
            rows[key] = decide(row, data, now, model, iso_utc(now))

    for row in rows.values():
        if row['state'] in ('issued', 'rest'):
            score_row(row, index, now)

    for k, before in frozen_before.items():
        if k in rows and frozen_view(rows[k]) != before:
            raise BriefUnavailable('frozen_row_changed')
    carried = trim_ledger(rows, carried)
    live = [r for r in rows.values() if r.get('origin') == 'live' and r['state'] != 'pending']
    current_row = max(live, key=lambda r: r['targetSession']) if live else None
    current = None
    if current_row is not None:
        current = ordered_row({**current_row, 'copy': build_copy(current_row, events_tonight(events, current_row, now))})
    return {
        'schemaVersion': SCHEMA_VERSION,
        'modelVersion': model.get('modelVersion'),
        'generatedAt': iso_utc(now),
        'status': 'ok',
        'phase': phase_for(current_row, now),
        'current': current,
        'ledger': [ordered_row(rows[k]) for k in sorted(rows)],
        'scorecard': build_scorecard(rows, carried),
        'nextIssueAt': iso_utc(issue_from(target if key not in rows or rows[key]['state'] == 'pending'
                                          else us_day_after(target))),
        'disclaimer': DISCLAIMER,
    }


def unavailable_block(previous_public: Mapping[str, Any] | None, now: datetime, error: BaseException) -> dict[str, Any]:
    reason = str(error) if isinstance(error, BriefUnavailable) else type(error).__name__
    previous = (previous_public or {}).get('eveningBrief')
    block = deepcopy(previous) if isinstance(previous, dict) and previous.get('schemaVersion') == SCHEMA_VERSION \
        else {'current': None, 'ledger': [], 'scorecard': None, 'nextIssueAt': None}
    block.update({'schemaVersion': SCHEMA_VERSION, 'modelVersion': MODEL_VERSION, 'generatedAt': iso_utc(as_aware(now)),
                  'status': 'unavailable', 'phase': 'off', 'reason': reason, 'disclaimer': DISCLAIMER})
    return block


def scheduled_events() -> list[dict[str, Any]]:
    """US releases from the snapshot's macro schedule (CPI, jobs, FOMC)."""
    import polarmeter_cache_snapshot as snap  # imported here: heavy module, only needed at run time
    names_en = {'us_nonfarm_payrolls': 'US jobs report', 'us_cpi': 'US CPI', 'fomc_rate': 'FOMC decision'}
    out = []
    for event_id, items in snap.SCHEDULED_MACRO_EVENTS.items():
        for at, label in items:
            out.append({'id': event_id, 'at': at, 'labelKo': f'{label} 발표', 'labelEn': names_en.get(event_id, label)})
    return out


def apply_evening_brief(snapshot: dict[str, Any], previous_public: Mapping[str, Any] | None,
                        now: datetime | None = None, fetch: Fetch | None = None) -> dict[str, Any]:
    """Attach `eveningBrief`. Never raises; on failure the block says why and keeps the old ledger."""
    now = as_aware(now) if now else datetime.now(timezone.utc)
    try:
        import polarmeter_v3_inputs as inputs
        model = load_model()
        if model.get('modelVersion') != MODEL_VERSION:
            raise BriefUnavailable('model_version_mismatch')
        data = fetch_inputs(fetch or inputs.fetch_json)
        block = compute_block(previous_public, data, now, model, scheduled_events())
        assert_public_evening_brief(block)
    except Exception as error:  # the brief must never stop a publish
        try:
            block = unavailable_block(previous_public, now, error)
        except Exception:  # pragma: no cover
            block = {'schemaVersion': SCHEMA_VERSION, 'modelVersion': MODEL_VERSION, 'generatedAt': iso_utc(now),
                     'status': 'unavailable', 'phase': 'off', 'reason': 'BriefError', 'current': None, 'ledger': [],
                     'scorecard': None, 'nextIssueAt': None, 'disclaimer': DISCLAIMER}
    snapshot['eveningBrief'] = block
    return snapshot


# ---------------------------------------------------------------- public-shape check

def assert_public_evening_brief(block: Any) -> None:
    assert isinstance(block, dict), 'eveningBrief must be an object'
    assert block.get('schemaVersion') == SCHEMA_VERSION, 'eveningBrief schemaVersion'
    assert block.get('status') in ('ok', 'unavailable'), 'eveningBrief status'
    assert block.get('phase') in PHASES, 'eveningBrief phase'
    assert isinstance(block.get('ledger'), list), 'eveningBrief ledger'
    for row in block['ledger'] + ([block['current']] if block.get('current') else []):
        assert valid_row(row), 'eveningBrief row'
        assert row.get('call') in CALLS + (None,), 'eveningBrief call'
        assert row.get('outcome') in OUTCOMES + (None,), 'eveningBrief outcome'
        assert row.get('restReason') in REST_REASONS + (None,), 'eveningBrief restReason'
        assert set(row) <= set(ROW_KEYS) | {'copy'}, 'eveningBrief row keys'
    assert isinstance(block.get('disclaimer'), dict), 'eveningBrief disclaimer'


def assert_public_evening_if_present(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get('eveningBrief') is not None:
        assert_public_evening_brief(snapshot['eveningBrief'])
