#!/usr/bin/env python3
"""Week outlook (꽁치의 7일 예보): a calendar-7-day view of KOSPI and the S&P 500, reissued at fixed slots.

What it says, and how sure each part is (10-year walk-forward, out/weekly-plan-20261127/ACCURACY.md):
  swing    how rough each of the next trading days is likely to be (stage 1-4) and the range that held the
           day's move 8 times in 10. Works out to day 5; this is the part that makes it a week view.
  cone     the band the whole path stayed inside 8 times in 10.
  open     KR only, morning slot: which side the session opens on.
  close    which side the session closes on, in three tiers: clear / lean / none. Never further out than
           the next session; the day after cannot be called (53%, below "always up").

Slots (KST). A market's entry is computed once per (target session, slot) and then carried unchanged, so the
public file shows the forecast as it was issued and the network is only used when a slot changes.
  KR  close  after the KR close        swing only for the next session
      2110   21:10                     + next-session close call from S&P futures since the KR close
      0010   00:10                     the same, with the US session under way
      after_us                         a closed day from 06:50, and the night after it: the same from the finished US session
      0650   06:50                     + open and close calls from the finished US session (the main issue)
      0905   09:05                     close call redone from the actual open
  US  close  after the US close        swing only for the next session
      2110   21:10                     + close call from S&P futures since the previous US close

Pure functions plus one worker entry point, `apply_week_outlook`, which never raises. The training script
(`polarmeter_week_outlook_train.py`) imports the feature functions below, so a backtested day and a live day
are computed by the same code.

Bar dates. Yahoo stamps a daily bar at the start of its session, in UTC. `KINDS` turns that into the local
session date. Futures and the dollar index are stamped at New York midnight, which is 04:00 UTC in summer:
read with a fixed -5h they land on the previous day and leak a whole session into the past. FX day bars end
at 09:00 KST and are not used here at all.
"""
from __future__ import annotations

import math
import urllib.parse
from bisect import bisect_left, bisect_right
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
import json

import polarmeter_market_calendar as cal

KST, NY = cal.KST_ZONE, cal.NY_ZONE
SCHEMA_VERSION = 'polarmeter-week-outlook-v1'
MODEL_VERSION = 'week-outlook-v1'
MODEL_PATH = Path(__file__).with_name('polarmeter_week_outlook_model.json')
DISCLAIMER = '과거 기록으로 계산한 예보이며 결과를 보장하지 않습니다. 투자 권유가 아닙니다.'
YAHOO_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}'
CALENDAR_DAYS = 7
HORIZONS = 5            # trading days inside 7 calendar days
BAND_WINDOW = 250       # sessions the 8-in-10 range is fitted on (a full-history fit ran narrow in 2026)
BAND_MIN = 120
BAND_QUANTILE = 0.8
FUTURES_STALE_HOURS = 100
FUTURES_SUSPECT_PCT = 7.0   # the continuous contract jumps on a roll; a move this size is not a call

# key: (Yahoo symbol, bar kind)
SYMBOLS = {
    'kr': ('^KS11', 'kr'), 'spx': ('^GSPC', 'us'), 'ndx': ('^IXIC', 'us'), 'sox': ('^SOX', 'us'), 'vix': ('^VIX', 'us'),
    'ewy': ('EWY', 'us'), 'tnx': ('^TNX', 'us'), 'hyg': ('HYG', 'us'), 'mu': ('MU', 'us'), 'nvda': ('NVDA', 'us'),
    'tsm': ('TSM', 'us'), 'dxy': ('DX-Y.NYB', 'fut'),
}
KINDS = {'us': timedelta(hours=-5), 'fut': timedelta(hours=-4), 'kr': timedelta(hours=9)}
KR_DIRECTION_FEATURES = (
    'us_spx', 'us_sox', 'us_ewy', 'us_ndx', 'vix_lvl', 'vix_chg', 'tnx_chg', 'hyg_chg', 'dxy_chg',
    'kr_prev', 'kr_5d', 'kr_dev20', 'kr_vol20', 'ewy_gap', 'us_mu', 'us_nvda', 'us_tsm',
    'us_intraday', 'us_closepos', 'ewy_closepos', 'kr_prev_intra', 'kr_prev_gap', 'kr_closepos', 'kr_dev60', 'gapdays')
SWING_FEATURES = ('abs5', 'abs20', 'abs60', 'hl5', 'hl20', 'vix_lvl')
OVERNIGHT_FEATURES = ('abs_us_spx', 'abs_us_sox', 'abs_ewy_gap')
Fetch = Callable[[str], Any]
Bar = tuple[float, float, float, float]   # open, high, low, close


class OutlookUnavailable(Exception):
    pass


# ---------------------------------------------------------------- bars

def parse_daily(payload: Mapping[str, Any], kind: str) -> dict[date, Bar]:
    result = payload['chart']['result'][0]
    quote = result['indicators']['quote'][0]
    shift = KINDS[kind]
    out: dict[date, Bar] = {}
    for stamp, o, h, l, c in zip(result.get('timestamp') or [], quote['open'], quote['high'], quote['low'], quote['close']):
        if not c:
            continue
        day = (datetime.fromtimestamp(stamp, timezone.utc) + shift).date()
        out[day] = (float(o or c), float(h or c), float(l or c), float(c))
    return out


def parse_hourly(payload: Mapping[str, Any]) -> list[tuple[int, float]]:
    """(bar end, close) pairs, oldest first. The last bar may still be trading; its close is the latest price."""
    result = payload['chart']['result'][0]
    closes = result['indicators']['quote'][0]['close']
    return [(int(stamp) + 3600, float(c)) for stamp, c in zip(result.get('timestamp') or [], closes) if c]


class Book:
    """Daily bars by key. Every lookup is "before this day", so a feature cannot read its own target."""

    def __init__(self, bars: Mapping[str, Mapping[date, Bar]]):
        self.bars = {key: dict(rows) for key, rows in bars.items()}
        self.dates = {key: sorted(rows) for key, rows in self.bars.items()}

    def before(self, key: str, day: date, n: int = 1) -> Bar | None:
        i = bisect_left(self.dates[key], day) - n
        return self.bars[key][self.dates[key][i]] if i >= 0 else None

    def last_date_before(self, key: str, day: date) -> date | None:
        i = bisect_left(self.dates[key], day) - 1
        return self.dates[key][i] if i >= 0 else None

    def window(self, key: str, day: date, n: int) -> list[Bar] | None:
        """The n bars right before `day`, oldest first."""
        i = bisect_left(self.dates[key], day)
        if i < n:
            return None
        return [self.bars[key][d] for d in self.dates[key][i - n:i]]

    def on(self, key: str, day: date) -> Bar | None:
        return self.bars[key].get(day)


def log_ratio(a: float, b: float) -> float:
    return math.log(a / b) if a > 0 and b > 0 else 0.0


def change(book: Book, key: str, day: date, prev: date, level: bool = False) -> float | None:
    """From the last close before `prev` to the last close before `day`."""
    a, b = book.before(key, day), book.before(key, prev)
    if a is None or b is None:
        return None
    return a[3] - b[3] if level else log_ratio(a[3], b[3])


def close_position(bar: Bar) -> float:
    return (bar[3] - bar[2]) / (bar[1] - bar[2] + 1e-9) - 0.5


def sd(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


# ---------------------------------------------------------------- features (shared with the training script)

def kr_direction_features(book: Book, day: date) -> dict[str, float] | None:
    """Inputs for a KR session `day`, known once the US session before it has closed."""
    rows = book.window('kr', day, 62)
    prev = book.last_date_before('kr', day)
    if rows is None or prev is None:
        return None
    closes = [row[3] for row in rows]
    rets = [log_ratio(closes[i], closes[i - 1]) for i in range(1, len(closes))]
    last, before_last = rows[-1], rows[-2]
    vix, spx, ewy = book.before('vix', day), book.before('spx', day), book.before('ewy', day)
    ewy_chg = change(book, 'ewy', day, prev)
    if vix is None or spx is None or ewy is None or ewy_chg is None:
        return None
    out: dict[str, float | None] = {
        'us_spx': change(book, 'spx', day, prev), 'us_sox': change(book, 'sox', day, prev), 'us_ewy': ewy_chg,
        'us_ndx': change(book, 'ndx', day, prev), 'vix_lvl': math.log(vix[3]), 'vix_chg': change(book, 'vix', day, prev),
        'tnx_chg': change(book, 'tnx', day, prev, level=True), 'hyg_chg': change(book, 'hyg', day, prev),
        'dxy_chg': change(book, 'dxy', day, prev),
        'kr_prev': rets[-1], 'kr_5d': log_ratio(closes[-1], closes[-6]),
        'kr_dev20': closes[-1] / (sum(closes[-21:-1]) / 20) - 1, 'kr_vol20': sd(rets[-20:]), 'ewy_gap': ewy_chg - rets[-1],
        'us_mu': change(book, 'mu', day, prev), 'us_nvda': change(book, 'nvda', day, prev), 'us_tsm': change(book, 'tsm', day, prev),
        'us_intraday': log_ratio(spx[3], spx[0]), 'us_closepos': close_position(spx), 'ewy_closepos': close_position(ewy),
        'kr_prev_intra': log_ratio(last[3], last[0]), 'kr_prev_gap': log_ratio(last[0], before_last[3]),
        'kr_closepos': close_position(last), 'kr_dev60': closes[-1] / (sum(closes[-61:-1]) / 60) - 1,
        'gapdays': float((day - prev).days - 1),
    }
    if any(value is None or not math.isfinite(value) for value in out.values()):
        return None
    return {name: float(out[name]) for name in KR_DIRECTION_FEATURES}  # type: ignore[arg-type]


def swing_features(book: Book, key: str, day: date) -> dict[str, float] | None:
    """How rough the market has been lately, known before `day` opens."""
    rows = book.window(key, day, 61)
    vix = book.before('vix', day)
    if rows is None or vix is None:
        return None
    absr = [abs(log_ratio(rows[i][3], rows[i - 1][3])) for i in range(1, len(rows))]
    hl = [math.log(row[1] / row[2]) / 1.665 if row[2] > 0 else 0.0 for row in rows]
    return {'abs5': sum(absr[-5:]) / 5, 'abs20': sum(absr[-20:]) / 20, 'abs60': sum(absr) / 60,
            'hl5': sum(hl[-5:]) / 5, 'hl20': sum(hl[-20:]) / 20, 'vix_lvl': math.log(vix[3])}


def overnight_features(direction: Mapping[str, float]) -> dict[str, float]:
    return {'abs_us_spx': abs(direction['us_spx']), 'abs_us_sox': abs(direction['us_sox']), 'abs_ewy_gap': abs(direction['ewy_gap'])}


def linear(part: Mapping[str, Any], values: Mapping[str, float]) -> float:
    """Standardised linear score: intercept + sum(coef * (x - mean) / sd)."""
    total = float(part['intercept'])
    for name, coef, mean, scale in zip(part['features'], part['coef'], part['mean'], part['sd']):
        total += coef * (values[name] - mean) / scale
    return total


def swing_value(part: Mapping[str, Any], values: Mapping[str, float]) -> float:
    return max(linear(part, values), float(part['floor']))


def stage_of(value: float, cuts: list[float]) -> int:
    return 1 + sum(1 for cut in cuts if value > cut)


def tier_of(magnitude: float, part: Mapping[str, Any]) -> str:
    """clear = the top fifth of past scores, lean = the next two fifths, none = the rest."""
    if magnitude >= part['clearAbove']:
        return 'clear'
    return 'lean' if magnitude >= part['leanAbove'] else 'none'


def quantile(values: list[float], q: float) -> float:
    rows = sorted(values)
    spot = (len(rows) - 1) * q
    low = int(math.floor(spot))
    high = min(low + 1, len(rows) - 1)
    return rows[low] + (rows[high] - rows[low]) * (spot - low)


# ---------------------------------------------------------------- calendar

def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def closed_reason(market: str, day: date) -> str | None:
    if market == 'kr':
        if day.year not in cal.KR_CALENDAR_YEARS:
            raise OutlookUnavailable('kr_calendar_unsupported')
        return cal.kr_market_closed_reason(datetime.combine(day, time(12), KST))
    return cal.us_market_closed_reason(datetime.combine(day, time(12), NY))


def next_open(market: str, day: date) -> date:
    for _ in range(20):
        if closed_reason(market, day) is None:
            return day
        day += timedelta(days=1)
    raise OutlookUnavailable('no_open_day')


def previous_open(market: str, day: date) -> date:
    day -= timedelta(days=1)
    for _ in range(20):
        if closed_reason(market, day) is None:
            return day
        day -= timedelta(days=1)
    raise OutlookUnavailable('no_open_day')


def us_close_at(day: date) -> datetime:
    return datetime.combine(day, cal.us_equity_close_time(datetime.combine(day, time(12), NY)), NY)


def kr_slot(now: datetime) -> tuple[date, str]:
    """(target KR session, slot) at `now`.

    A night that follows a closed day (a weekend, a holiday) has no US session of its own under way, so the
    futures view is the finished one: `after_us`. It is first issued at 06:50 on the closed day, once the last
    US session has closed, never in the middle of that session.
    """
    local = now.astimezone(KST)
    today, clock = local.date(), local.time()
    open_today = closed_reason('kr', today) is None
    open_yesterday = closed_reason('kr', today - timedelta(days=1)) is None
    if open_today and clock >= time(15, 40):
        return next_open('kr', today + timedelta(days=1)), '2110' if clock >= time(21, 10) else 'close'
    target = today if open_today else next_open('kr', today)
    if open_today and clock >= time(9, 5):
        return target, '0905'
    if open_today and clock >= time(6, 50):
        return target, '0650'
    if clock < time(6, 50) and open_yesterday:      # the night after an open day: its US session is under way
        return target, '0010' if clock >= time(0, 10) else '2110'
    return target, 'after_us'


def us_slot(now: datetime) -> tuple[date, str]:
    """(target US session, slot) at `now`."""
    ny = now.astimezone(NY)
    today = ny.date()
    if closed_reason('us', today) is None and now < us_close_at(today):
        target = today
    else:
        target = next_open('us', today + timedelta(days=1))
    issue = datetime.combine(target, time(21, 10), KST)   # 21:10 KST on the session's own calendar date
    return target, '2110' if now >= issue else 'close'


# ---------------------------------------------------------------- futures

def price_at(bars: list[tuple[int, float]], moment: datetime) -> float | None:
    """The last hourly close at or before `moment` (not older than FUTURES_STALE_HOURS)."""
    stamp = int(moment.timestamp())
    i = bisect_right([end for end, _ in bars], stamp) - 1
    if i < 0 or stamp - bars[i][0] > FUTURES_STALE_HOURS * 3600:
        return None
    return bars[i][1]


def futures_move(bars: list[tuple[int, float]], since: datetime, now: datetime) -> float | None:
    """S&P futures % move from `since` to `now`; None when either end is missing or the move looks like a roll."""
    a, b = price_at(bars, since), price_at(bars, now)
    if a is None or b is None or a <= 0:
        return None
    move = (b / a - 1) * 100
    return None if abs(move) >= FUTURES_SUSPECT_PCT else move


TIER_EDGE_PCT = 3.0   # a tier is only spoken when its record beats "always up" by this much


def call_from(score: float, magnitude: float, part: Mapping[str, Any], basis: str) -> dict[str, Any]:
    tier = tier_of(magnitude, part)
    side = 'up' if score > 0 else 'down'
    hit = part['hitPct'].get(tier)
    if tier != 'none' and (hit is None or hit < part['alwaysUpPct'] + TIER_EDGE_PCT):
        tier = 'none'   # e.g. the 21:10 futures "lean" tier: 54.5% against 57.7% for always-up
    return {'call': side if tier != 'none' else 'none', 'tier': tier, 'lean': side, 'basis': basis,
            'hitPct': part['hitPct'].get(tier), 'sampleDays': part.get('sampleDays')}


def no_call(basis: str) -> dict[str, Any]:
    """A slot that gives no direction on purpose; `basis` says why."""
    return {'call': 'none', 'tier': 'none', 'lean': None, 'basis': basis, 'hitPct': None, 'sampleDays': None}


# ---------------------------------------------------------------- one market

def band_multiplier(book: Book, key: str, part: Mapping[str, Any], extra: Callable[[date], dict[str, float] | None] | None,
                    before: date) -> float:
    """What the predicted swing must be multiplied by so that the day's move stayed inside 8 times in 10,
    measured on the sessions right before `before`."""
    ratios: list[float] = []
    for day in reversed(book.dates[key][:bisect_left(book.dates[key], before)]):
        values = swing_features(book, key, day)
        prev = book.before(key, day)
        if values is None or prev is None:
            break
        if extra is not None:
            more = extra(day)
            if more is None:
                continue
            values = {**values, **more}
        ratios.append(abs(log_ratio(book.bars[key][day][3], prev[3])) / swing_value(part, values))
        if len(ratios) >= BAND_WINDOW:
            break
    if len(ratios) < BAND_MIN:
        raise OutlookUnavailable('band_history_short')
    return quantile(ratios, BAND_QUANTILE)


def session_days(market: str, target: date, book: Book, key: str, model: Mapping[str, Any],
                 overnight: Mapping[str, float] | None) -> list[dict[str, Any]]:
    """Seven calendar days from `target`: closed days say why, open days carry swing stage, range and cone."""
    part = model[market]['swing']
    base = swing_features(book, key, target)
    if base is None:
        raise OutlookUnavailable('swing_inputs_missing')
    multiplier = band_multiplier(book, key, part['horizons'][0], None, target)
    days: list[dict[str, Any]] = []
    variance, h = 0.0, 0
    for offset in range(CALENDAR_DAYS):
        day = target + timedelta(days=offset)
        row: dict[str, Any] = {'date': day.isoformat(), 'weekday': day.weekday()}
        reason = closed_reason(market, day)
        if reason is not None or h >= HORIZONS:
            row.update({'closed': True, 'closedReason': reason or 'beyond_horizon'})
            days.append(row)
            continue
        horizon = part['horizons'][h]
        value = swing_value(horizon, base)
        used = multiplier
        if h == 0 and overnight is not None:
            first = part['overnight']
            value = swing_value(first, {**base, **overnight})
            used = band_multiplier(book, key, first, lambda d: _overnight_for(book, d), target)
            horizon = first
        variance += value ** 2
        stage = stage_of(value, horizon['stageCuts'])
        row.update({'closed': False, 'sessionsAhead': h, 'swingStage': stage,
                    'rangePct': round(value * used * 100, 2),
                    'conePct': round(math.sqrt(variance) * part['pathMultiplier'] * 100, 2),
                    'stageRecord': horizon['stageRecord'][stage - 1]})
        days.append(row)
        h += 1
    return days


def _overnight_for(book: Book, day: date) -> dict[str, float] | None:
    values = kr_direction_features(book, day)
    return overnight_features(values) if values else None


def kr_entry(target: date, slot: str, book: Book, futures: list[tuple[int, float]], model: Mapping[str, Any],
             now: datetime) -> dict[str, Any]:
    part = model['kr']
    basis = book.last_date_before('kr', target)
    if basis is None or basis != previous_open('kr', target):
        raise OutlookUnavailable('kr_basis_bar_missing')
    entry: dict[str, Any] = {'target': target.isoformat(), 'slot': slot, 'issuedAt': iso_utc(now), 'basisSession': basis.isoformat(),
                             'open': None, 'close': None}
    overnight = None
    if slot in ('0650', '0905'):
        expected = previous_open('us', target)
        if book.last_date_before('spx', target) != expected:
            raise OutlookUnavailable('us_close_bar_missing')
        values = kr_direction_features(book, target)
        if values is None:
            raise OutlookUnavailable('direction_inputs_missing')
        if expected < basis:
            # The US market was shut the night before (e.g. Thanksgiving): its last close was already priced into the
            # Korean basis session, so nothing new came in overnight. The morning card says the same and gives no call;
            # the week outlook must not quietly reuse a two-day-old US session as if it were last night's.
            entry['open'] = no_call('us_closed')
            entry['close'] = no_call('us_closed')
        else:
            overnight = overnight_features(values)
            gap = linear(part['open'], values)
            entry['open'] = call_from(gap, abs(gap), part['open'], 'us_session')
            score = linear(part['close'], values)
            entry['close'] = call_from(score, abs(score), part['close'], 'us_session')
        if slot == '0905':
            today_bar, prev_bar = book.on('kr', target), book.before('kr', target)
            if today_bar is None or prev_bar is None:
                raise OutlookUnavailable('kr_open_missing')
            actual_gap = (today_bar[0] / prev_bar[3] - 1) * 100
            entry['open'] = {**entry['open'], 'actualPct': round(actual_gap, 2)}
            entry['close'] = call_from(actual_gap, abs(actual_gap), part['closeAfterOpen'], 'actual_open')
    elif slot in ('2110', '0010', 'after_us'):
        since = datetime.combine(basis, time(15, 30), KST)
        move = futures_move(futures, since, now)
        if move is not None:
            entry['close'] = {**call_from(move, abs(move), part['futures'][slot], 'sp_futures_since_kr_close'),
                              'futuresPct': round(move, 2)}
    entry['days'] = session_days('kr', target, book, 'kr', model, overnight)
    return entry


def us_entry(target: date, slot: str, book: Book, futures: list[tuple[int, float]], model: Mapping[str, Any],
             now: datetime) -> dict[str, Any]:
    basis = book.last_date_before('spx', target)
    if basis is None or basis != previous_open('us', target):
        raise OutlookUnavailable('us_basis_bar_missing')
    entry: dict[str, Any] = {'target': target.isoformat(), 'slot': slot, 'issuedAt': iso_utc(now), 'basisSession': basis.isoformat(),
                             'open': None, 'close': None}
    if slot == '2110':
        move = futures_move(futures, us_close_at(basis), now)
        if move is not None:
            entry['close'] = {**call_from(move, abs(move), model['us']['futures']['2110'], 'sp_futures_since_us_close'),
                              'futuresPct': round(move, 2)}
    entry['days'] = session_days('us', target, book, 'spx', model, None)
    return entry


# ---------------------------------------------------------------- block

def load_model(path: Path | str = MODEL_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def fetch_book(fetch: Fetch) -> tuple[Book, list[tuple[int, float]]]:
    bars = {}
    for key, (symbol, kind) in SYMBOLS.items():
        url = YAHOO_URL.format(symbol=urllib.parse.quote(symbol, safe=''), range='2y', interval='1d')
        bars[key] = parse_daily(fetch(url), kind)
    futures = parse_hourly(fetch(YAHOO_URL.format(symbol=urllib.parse.quote('ES=F', safe=''), range='1mo', interval='1h')))
    return Book(bars), futures


def previous_entry(previous_public: Mapping[str, Any] | None, market: str) -> dict[str, Any] | None:
    block = (previous_public or {}).get('weekOutlook')
    if not isinstance(block, Mapping) or block.get('modelVersion') != MODEL_VERSION:
        return None
    entry = (block.get('markets') or {}).get(market)
    return dict(entry) if isinstance(entry, Mapping) and entry.get('target') and entry.get('slot') else None


def compute_block(previous_public: Mapping[str, Any] | None, now: datetime, model: Mapping[str, Any],
                  load: Callable[[], tuple[Book, list[tuple[int, float]]]]) -> dict[str, Any]:
    block, book, fresh = _compute_markets(previous_public, now, model, load)
    old = (previous_public or {}).get('weekOutlook')
    old_track = old.get('track') if isinstance(old, Mapping) else None
    old_regime = old.get('regime') if isinstance(old, Mapping) else None
    block['track'] = update_track(old_track, previous_public, block['markets'], fresh, book, now)
    block['regime'] = regime_of(book, block['markets'], old_regime)
    return block


def _compute_markets(previous_public: Mapping[str, Any] | None, now: datetime, model: Mapping[str, Any],
                     load: Callable[[], tuple[Book, list[tuple[int, float]]]]
                     ) -> tuple[dict[str, Any], Book | None, set[str]]:
    """Recompute only the markets whose (target, slot) changed; `load` is called at most once and only then."""
    wanted = {'kr': kr_slot(now), 'us': us_slot(now)}
    markets: dict[str, Any] = {}
    waiting: dict[str, str] = {}
    fresh: set[str] = set()
    data: tuple[Book, list[tuple[int, float]]] | None = None
    for market, (target, slot) in wanted.items():
        old = previous_entry(previous_public, market)
        if old and old['target'] == target.isoformat() and old['slot'] == slot:
            markets[market] = old
            continue
        try:
            if data is None:
                data = load()
            build = kr_entry if market == 'kr' else us_entry
            markets[market] = build(target, slot, data[0], data[1], model, now)
            fresh.add(market)
        except OutlookUnavailable as error:
            # Not ready yet (a bar has not arrived): keep what was issued and try again on the next run.
            waiting[market] = str(error)
            if old:
                markets[market] = old
    if not markets:
        raise OutlookUnavailable(next(iter(waiting.values()), 'no_market'))
    return {'schemaVersion': SCHEMA_VERSION, 'modelVersion': MODEL_VERSION, 'generatedAt': iso_utc(now),
            'status': 'ready' if not waiting else 'partial', 'waiting': waiting or None, 'markets': markets,
            'record': model['record'], 'disclaimer': DISCLAIMER}, (data[0] if data else None), fresh


# ---------------------------------------------------------------- live track: what was said, what happened, what changed

TRACK_ROWS_PER_MARKET = 40
TRACK_REVISIONS = 40
SETTLE_AFTER_CLOSE = timedelta(hours=2)      # a daily bar read right at the bell can still move
BOOK_KEY = {'kr': 'kr', 'us': 'spx'}
REGIME_RECENT, REGIME_USUAL = 5, 20


def _call_label(call: Mapping[str, Any] | None) -> str | None:
    """'up:clear', 'down:lean', 'none', or None when nothing was said at that slot."""
    if not call:
        return None
    return 'none' if call['call'] == 'none' else f"{call['call']}:{call['tier']}"


def _close_at(market: str, day: date) -> datetime:
    return datetime.combine(day, time(15, 30), KST) if market == 'kr' else us_close_at(day)


def _hit(label: str | None, pct: float) -> bool | None:
    """True/False for a said direction; None when nothing was said or the session ended flat."""
    if not label or label == 'none' or pct == 0:
        return None
    return (label.split(':')[0] == 'up') == (pct > 0)


def update_track(old: Any, previous_public: Mapping[str, Any] | None, markets: Mapping[str, Any], fresh: set[str],
                 book: Book | None, now: datetime) -> dict[str, Any]:
    """The live ledger carried inside the block: each issue as it was made, the result once the session has settled,
    and every change between two issues. Nothing here is recomputed afterwards — an issue is written once."""
    track = deepcopy(dict(old)) if isinstance(old, Mapping) and old.get('rows') is not None else {
        'since': now.astimezone(KST).date().isoformat(), 'rows': [], 'revisions': [], 'dayLog': {}}
    rows: list[dict[str, Any]] = track['rows']
    revisions: list[dict[str, Any]] = track['revisions']
    day_log: dict[str, dict[str, int]] = track.setdefault('dayLog', {})
    stamp = iso_utc(now)
    # Every market, not only the ones rebuilt in this run: an entry carried from before the ledger existed is
    # logged once too (the slot check below keeps it to one issue per slot).
    for market in sorted(markets):
        entry = markets[market]
        first = entry['days'][0]
        issue = {'slot': entry['slot'], 'at': entry['issuedAt'], 'open': _call_label(entry['open']),
                 'close': _call_label(entry['close']), 'stage': first['swingStage'], 'rangePct': first['rangePct']}
        row = next((r for r in rows if r['market'] == market and r['target'] == entry['target']), None)
        if row is None:
            row = {'market': market, 'target': entry['target'], 'issues': [], 'actual': None}
            rows.append(row)
        last = row['issues'][-1] if row['issues'] else None
        if last and last['slot'] == issue['slot']:
            continue
        if last:
            for kind in ('open', 'close'):
                # A direction that was said and is now said differently (the first time it is said is not a change).
                if last[kind] is not None and issue[kind] is not None and last[kind] != issue[kind]:
                    revisions.append({'market': market, 'date': entry['target'], 'at': stamp, 'slot': issue['slot'],
                                      'kind': kind, 'from': last[kind], 'to': issue[kind]})
        row['issues'].append(issue)
        seen = day_log.setdefault(market, {})
        for day in entry['days']:
            if day['closed']:
                continue
            before = seen.get(day['date'])
            if before is not None and before != day['swingStage']:
                revisions.append({'market': market, 'date': day['date'], 'at': stamp, 'slot': issue['slot'],
                                  'kind': 'swing', 'from': before, 'to': day['swingStage']})
            seen[day['date']] = day['swingStage']
        for stale in [d for d in seen if d < entry['target']]:
            del seen[stale]
    if book is not None:
        for row in rows:
            entry = markets.get(row['market'])
            if row['actual'] is not None or entry is None or row['target'] >= entry['target']:
                continue
            day = date.fromisoformat(row['target'])
            key = BOOK_KEY[row['market']]
            bar, prev = book.on(key, day), book.before(key, day)
            if bar is None or prev is None or now < _close_at(row['market'], day) + SETTLE_AFTER_CLOSE:
                continue
            open_pct = round((bar[0] / prev[3] - 1) * 100, 2)
            close_pct = round((bar[3] / prev[3] - 1) * 100, 2)
            row['actual'] = {'openPct': open_pct, 'closePct': close_pct}
            for issue in row['issues']:
                issue['closeHit'] = _hit(issue['close'], close_pct)
                # Only a forecast made before the bell is scored for the open (09:05 already carries the real open).
                issue['openHit'] = _hit(issue['open'], open_pct) if issue['slot'] != '0905' else None
            row['rangeInside'] = abs(close_pct) <= row['issues'][-1]['rangePct'] if row['issues'] else None
    for market in SLOTS:
        mine = [r for r in rows if r['market'] == market]
        for extra in mine[:-TRACK_ROWS_PER_MARKET]:
            rows.remove(extra)
    del revisions[:-TRACK_REVISIONS]
    track['totals'] = track_totals(rows)
    return track


def track_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per market: range days and, per slot, how many said directions there were and how many were right."""
    totals: dict[str, Any] = {}
    for market in SLOTS:
        scored = [r for r in rows if r['market'] == market and r['actual'] is not None]
        slots: dict[str, dict[str, int]] = {}
        for row in scored:
            for issue in row['issues']:
                cell = slots.setdefault(issue['slot'], {'closeCalls': 0, 'closeHits': 0, 'openCalls': 0, 'openHits': 0})
                for kind in ('close', 'open'):
                    hit = issue.get(f'{kind}Hit')
                    if hit is not None:
                        cell[f'{kind}Calls'] += 1
                        cell[f'{kind}Hits'] += int(hit)
        ranged = [r for r in scored if r.get('rangeInside') is not None]
        totals[market] = {'sessions': len(scored), 'rangeDays': len(ranged),
                          'rangeInside': sum(1 for r in ranged if r['rangeInside']), 'slots': slots}
    return totals


def regime_of(book: Book | None, markets: Mapping[str, Any], old: Any) -> dict[str, Any] | None:
    """How much each index moved a day over its last five sessions against the twenty before them."""
    result = dict(old) if isinstance(old, Mapping) else {}
    if book is None:
        return result or None
    for market, entry in markets.items():
        key = BOOK_KEY[market]
        bars = book.window(key, date.fromisoformat(entry['target']), REGIME_RECENT + REGIME_USUAL + 1)
        if not bars:
            continue
        moves = [abs(bars[i][3] / bars[i - 1][3] - 1) * 100 for i in range(1, len(bars))]
        recent = sum(moves[-REGIME_RECENT:]) / REGIME_RECENT
        usual = sum(moves[:REGIME_USUAL]) / REGIME_USUAL
        if usual <= 0:
            continue
        ratio = recent / usual
        result[market] = {'recentPct': round(recent, 2), 'usualPct': round(usual, 2), 'through': entry['basisSession'],
                          'state': 'rougher' if ratio >= 1.25 else 'calmer' if ratio <= 0.8 else 'same'}
    return result or None


def unavailable_block(now: datetime, error: BaseException) -> dict[str, Any]:
    reason = str(error) if isinstance(error, OutlookUnavailable) else type(error).__name__
    return {'schemaVersion': SCHEMA_VERSION, 'modelVersion': MODEL_VERSION, 'generatedAt': iso_utc(now),
            'status': 'unavailable', 'reason': reason, 'waiting': None, 'markets': {}, 'record': None, 'disclaimer': DISCLAIMER,
            'track': None, 'regime': None}


def apply_week_outlook(snapshot: dict[str, Any], previous_public: Mapping[str, Any] | None, now: datetime | None = None,
                       fetch: Fetch | None = None) -> dict[str, Any]:
    """Attach `weekOutlook` to the snapshot. Never raises; when it cannot compute it keeps the last issued block."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        import polarmeter_v3_inputs as inputs   # imported here: that module imports this one at load time
        model = load_model()
        if model.get('modelVersion') != MODEL_VERSION:
            raise OutlookUnavailable('model_version_mismatch')
        block = compute_block(previous_public, now, model, lambda: fetch_book(fetch or inputs.fetch_json))
        assert_public_week_outlook(block)
    except Exception as error:   # the outlook layer must never stop a publish
        old = (previous_public or {}).get('weekOutlook')
        block = unavailable_block(now, error)
        if isinstance(old, Mapping) and isinstance(old.get('track'), Mapping):
            block['track'] = old['track']      # the ledger outlives a model change or an outage
        if isinstance(old, Mapping) and old.get('modelVersion') == MODEL_VERSION and old.get('markets'):
            block = {**dict(old), 'generatedAt': iso_utc(now), 'status': 'stale', 'reason': block['reason']}
    snapshot['weekOutlook'] = block
    return snapshot


# ---------------------------------------------------------------- public-shape check

BLOCK_KEYS = {'schemaVersion', 'modelVersion', 'generatedAt', 'status', 'waiting', 'markets', 'record', 'disclaimer', 'reason',
              'track', 'regime'}
TRACK_KEYS = {'since', 'rows', 'revisions', 'dayLog', 'totals'}
ISSUE_KEYS = {'slot', 'at', 'open', 'close', 'stage', 'rangePct', 'closeHit', 'openHit'}
CALL_LABELS = {None, 'none', 'up:clear', 'up:lean', 'down:clear', 'down:lean'}
ENTRY_KEYS = {'target', 'slot', 'issuedAt', 'basisSession', 'open', 'close', 'days'}
CALL_KEYS = {'call', 'tier', 'lean', 'basis', 'hitPct', 'sampleDays', 'futuresPct', 'actualPct'}
OPEN_DAY_KEYS = {'date', 'weekday', 'closed', 'sessionsAhead', 'swingStage', 'rangePct', 'conePct', 'stageRecord'}
CLOSED_DAY_KEYS = {'date', 'weekday', 'closed', 'closedReason'}
SLOTS = {'kr': {'close', '2110', '0010', 'after_us', '0650', '0905'}, 'us': {'close', '2110'}}


def assert_public_week_outlook(block: Any) -> None:
    assert isinstance(block, dict) and set(block) <= BLOCK_KEYS, 'weekOutlook: unexpected keys'
    assert block['schemaVersion'] == SCHEMA_VERSION and block['status'] in {'ready', 'partial', 'stale', 'unavailable'}
    assert block['disclaimer'] == DISCLAIMER
    for market, entry in (block.get('markets') or {}).items():
        assert market in SLOTS and set(entry) == ENTRY_KEYS, f'weekOutlook.{market}: unexpected keys'
        assert entry['slot'] in SLOTS[market]
        for name in ('open', 'close'):
            call = entry[name]
            if call is None:
                continue
            assert set(call) <= CALL_KEYS and call['call'] in {'up', 'down', 'none'} and call['tier'] in {'clear', 'lean', 'none'}
            assert (call['call'] == 'none') == (call['tier'] == 'none'), 'a call needs a tier and a tier needs a call'
        days = entry['days']
        assert len(days) == CALENDAR_DAYS and days[0]['date'] == entry['target'] and not days[0]['closed']
        cone = 0.0
        for row in days:
            if row['closed']:
                assert set(row) == CLOSED_DAY_KEYS
                continue
            assert set(row) == OPEN_DAY_KEYS and 1 <= row['swingStage'] <= 4 and row['rangePct'] > 0
            assert row['conePct'] >= cone, 'the cone never narrows further out'
            cone = row['conePct']
        assert sum(1 for row in days if not row['closed']) <= HORIZONS
    track = block.get('track')
    if track is not None:
        assert set(track) == TRACK_KEYS, 'weekOutlook.track: unexpected keys'
        for row in track['rows']:
            assert row['market'] in SLOTS and set(row) <= {'market', 'target', 'issues', 'actual', 'rangeInside'}
            for issue in row['issues']:
                assert set(issue) <= ISSUE_KEYS and issue['slot'] in SLOTS[row['market']]
                assert issue['open'] in CALL_LABELS and issue['close'] in CALL_LABELS
                if row['actual'] is None:
                    assert 'closeHit' not in issue, 'an unsettled session carries no result'
        for change in track['revisions']:
            assert set(change) == {'market', 'date', 'at', 'slot', 'kind', 'from', 'to'}
            assert change['kind'] in {'open', 'close', 'swing'} and change['from'] != change['to']
    regime = block.get('regime')
    if regime is not None:
        for market, row in regime.items():
            assert market in SLOTS and row['state'] in {'rougher', 'same', 'calmer'} and row['usualPct'] > 0


def assert_public_week_outlook_if_present(snapshot: Mapping[str, Any]) -> None:
    block = snapshot.get('weekOutlook')
    if block is not None:
        assert_public_week_outlook(block)
