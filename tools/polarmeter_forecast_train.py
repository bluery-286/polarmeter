#!/usr/bin/env python3
"""Train and walk-forward test the morning forecast model (SPEC §6.2, bundle 1-A).

Run by a person, never by the cache workflow. Raw downloads and intermediate
files go to --data-dir (outside the repository); the model goes to
tools/polarmeter_forecast_model.json and the Korean report to --report.

  PYTHONPATH=tools python3 tools/polarmeter_forecast_train.py \
      --data-dir <out>/train-data --report <out>/train-report.md --fetch

Sources (public, delayed, free):
  KOSPI open/close  Naver daily index rows  (openPrice, closePrice)
  KOSPI cross-check Yahoo ^KS11 daily close (days off by >0.3% are dropped)
  SPY, SOXX, EWY    Yahoo daily bars, exchange-local dates

Every number in the model except the coefficients comes from walk-forward
predictions: each day is predicted by a formula fitted only on earlier days
(at least MIN_TRAIN_DAYS of them). The coefficients are refitted once on all
days at the end. Inputs come from polarmeter_morning_forecast.forecast_inputs,
the same function the live layer uses.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polarmeter_morning_forecast as mf
import polarmeter_score_core_v3 as v3
from polarmeter_market_calendar import KST_ZONE, NY_ZONE, last_completed_session_date, us_equity_close_time
from polarmeter_v3_inputs import REQUEST_HEADERS, fetch_json, number, series_from_chart

NAVER_DAILY_URL = 'https://m.stock.naver.com/api/index/KOSPI/price?pageSize={size}&page={page}'
NAVER_PAGE_SIZE = 60  # the endpoint refuses 100
NAVER_MAX_PAGES = 40
YAHOO_5Y_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5y&interval=1d'
YAHOO_SYMBOLS = ('SPY', 'SOXX', 'EWY', '^KS11')
REQUEST_PAUSE = 0.35
CROSS_CHECK_TOLERANCE = 0.003
MIN_TRAIN_DAYS = 500
OPEN_TARGET_HIT = 80.0
CLOSE_TARGET_HIT = 75.0
STRONG_THRESHOLD = 1.0
STOP_OVERALL_OPEN_HIT = 78.0
STOP_YEAR_OPEN_HIT = 70.0
MIN_YEAR_CALLS = 30  # a year with fewer open calls is reported, not judged (SPEC §6.2, revised 2026-11-06)
GAP_BIG = 1.5
SUSPECT_INPUT_ABS = 15.0
PRELIM = {'open': {'intercept': 0.067, 'coef': [0.180, 0.199, 0.077]},
          'close': {'intercept': -0.009, 'coef': [0.139, 0.237, 0.063]}}
CASE_20261106 = {'T': '2026-11-06', 'ewyExcessPct': -8.18, 'spyPct': -4.41, 'soxxPct': -12.15,
                 'open': 5263.26, 'prevClose': 5383.80, 'gapPct': -2.24}


# ---------------------------------------------------------------- download

def fetch_all(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc)
    for symbol in YAHOO_SYMBOLS:
        payload = fetch_json(YAHOO_5Y_URL.format(symbol=urllib.parse.quote(symbol, safe='')), timeout=30)
        (data_dir / f'yahoo_{safe_name(symbol)}.json').write_text(json.dumps(payload), encoding='utf-8')
        print('yahoo', symbol, len((payload['chart']['result'][0].get('timestamp') or [])))
        time.sleep(REQUEST_PAUSE)
    first_us = min(series_from_chart(json.loads((data_dir / 'yahoo_SPY.json').read_text())).dates)
    rows: list[dict[str, Any]] = []
    for page in range(1, NAVER_MAX_PAGES + 1):
        chunk = fetch_json(NAVER_DAILY_URL.format(size=NAVER_PAGE_SIZE, page=page), timeout=30)
        time.sleep(REQUEST_PAUSE)
        if not isinstance(chunk, list) or not chunk:
            break
        rows.extend(chunk)
        if min(str(r.get('localTradedAt')) for r in chunk) < first_us:
            break
    (data_dir / 'naver_kospi_daily.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    (data_dir / 'fetch_meta.json').write_text(json.dumps({
        'fetchedAt': fetched_at.isoformat().replace('+00:00', 'Z'), 'naverRows': len(rows),
        'naverPages': page, 'userAgent': REQUEST_HEADERS['User-Agent']}, indent=1), encoding='utf-8')
    print('naver rows', len(rows), 'pages', page)


def safe_name(symbol: str) -> str:
    return symbol.replace('^', '_').replace('=', '_')


# ---------------------------------------------------------------- load

def load_data(data_dir: Path) -> dict[str, Any]:
    meta = json.loads((data_dir / 'fetch_meta.json').read_text())
    fetched_at = datetime.fromisoformat(meta['fetchedAt'].replace('Z', '+00:00'))
    last_us = last_completed_session_date(fetched_at, 'US').isoformat()
    last_kr = last_completed_session_date(fetched_at, 'KR').isoformat()
    us: dict[str, v3.Series] = {}
    yahoo_raw: dict[str, dict[str, Any]] = {}
    for symbol in YAHOO_SYMBOLS:
        payload = json.loads((data_dir / f'yahoo_{safe_name(symbol)}.json').read_text())
        series = series_from_chart(payload)
        cutoff = last_kr if symbol == '^KS11' else last_us
        # Drop any bar after the last completed session at fetch time (a live bar is not a close).
        us[symbol] = v3.Series((d, c) for d, c in zip(series.dates, series.closes) if d <= cutoff)
        yahoo_raw[symbol] = yahoo_ohlc(payload, cutoff)
    naver: dict[str, dict[str, float | None]] = {}
    for row in json.loads((data_dir / 'naver_kospi_daily.json').read_text()):
        day = str(row.get('localTradedAt') or '')
        if len(day) == 10 and day <= last_kr:
            naver[day] = {'open': number(row.get('openPrice')), 'close': number(row.get('closePrice'))}
    return {'meta': meta, 'fetchedAt': fetched_at, 'lastUs': last_us, 'lastKr': last_kr, 'us': us,
            'naver': naver, 'yahooRaw': yahoo_raw}


def yahoo_ohlc(payload: dict[str, Any], cutoff: str) -> dict[str, tuple[float | None, float | None]]:
    """Exchange-local date -> (open, close) straight from the bars, for independent checks."""
    result = payload['chart']['result'][0]
    zone = ZoneInfo(result['meta'].get('exchangeTimezoneName') or 'UTC')
    quote = result['indicators']['quote'][0]
    out = {}
    for ts, o, c in zip(result.get('timestamp') or [], quote.get('open') or [], quote.get('close') or []):
        day = datetime.fromtimestamp(ts, zone).date().isoformat()
        if day <= cutoff:
            out[day] = (o, c)
    return out


# ---------------------------------------------------------------- samples

def cross_check(naver: dict[str, dict], ks11: v3.Series) -> dict[str, Any]:
    yahoo = dict(zip(ks11.dates, ks11.closes))
    bad, unverified, diffs = {}, [], []
    for day, row in sorted(naver.items()):
        y = yahoo.get(day)
        if row['close'] is None:
            bad[day] = 'naver_close_missing'
            continue
        if y is None:
            unverified.append(day)
            continue
        diff = row['close'] / y - 1
        diffs.append(abs(diff))
        if abs(diff) > CROSS_CHECK_TOLERANCE:
            bad[day] = f'naver {row["close"]:.2f} vs yahoo {y:.2f} ({diff * 100:+.2f}%)'
    yahoo_only = sorted(d for d in yahoo if d not in naver and d >= min(naver) and yahoo[d] is not None)
    return {'bad': bad, 'unverified': unverified, 'yahooOnly': yahoo_only,
            'maxAbsDiffPct': max(diffs) * 100 if diffs else None, 'checked': len(diffs)}


def build_samples(data: dict[str, Any], check: dict[str, Any]) -> tuple[list[dict], Counter, dict[str, list]]:
    naver = data['naver']
    kospi = v3.Series((d, r['close']) for d, r in naver.items())
    us = {s: data['us'][s] for s in ('SPY', 'SOXX', 'EWY')}
    first_us = us['SPY'].dates[0]
    days = sorted(naver)
    skipped: Counter = Counter()
    skipped_days: dict[str, list] = defaultdict(list)
    samples = []
    for k in range(2, len(days)):
        T, P, PP = days[k], days[k - 1], days[k - 2]
        if P < first_us:
            continue
        fi = mf.forecast_inputs(T, P, us, kospi)
        if fi['reason']:
            skipped[fi['reason']] += 1
            skipped_days[fi['reason']].append(T)
            continue
        kr_in = fi['krSessionsInEwy']
        first_kr = days.index(kr_in[0]) - 1 if kr_in else days.index(P)
        used_kr = [T] + days[first_kr:days.index(P) + 1]
        bad = [d for d in used_kr if d in check['bad']]
        if bad:
            skipped['kr_price_mismatch'] += 1
            skipped_days['kr_price_mismatch'].append(T)
            continue
        o, c, pc = naver[T]['open'], naver[T]['close'], naver[P]['close']
        if not o or not c or not pc:
            skipped['kr_open_missing'] += 1
            skipped_days['kr_open_missing'].append(T)
            continue
        # The first design (before 2026-11-06) subtracted only P's own move, and only when P was a US session.
        p_in = P in fi['usSessions']
        old_prev = round((naver[P]['close'] / naver[PP]['close'] - 1) * 100, mf.INPUT_DECIMALS) if p_in else 0.0
        samples.append({
            'T': T, 'P': P, 'year': T[:4], 'usSessions': fi['usSessions'], 'usBase': fi['usBaseSession'],
            'krInEwy': kr_in, 'inputs': fi['inputs'], 'gap': (o / pc - 1) * 100, 'c2c': (c / pc - 1) * 100,
            'o2c': (c / o - 1) * 100, 'openEqPrev': abs(o / pc - 1) < 0.0001,
            'krHolidayGap': business_days_between(P, T) > 0,
            'oldEwyExcessPct': round(fi['inputs']['ewyPct'] - old_prev, mf.INPUT_DECIMALS),
            'oldRuleDiffers': len(kr_in) != (1 if p_in else 0),
        })
    return samples, skipped, skipped_days


def business_days_between(a: str, b: str) -> int:
    """Weekdays strictly between two dates (a KR holiday sits there if > 0)."""
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    return sum(1 for n in range(1, (db - da).days) if date.fromordinal(da.toordinal() + n).weekday() < 5)


# ---------------------------------------------------------------- least squares

def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small dense system)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError('singular normal equations')
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for j in range(col, n + 1):
                    m[r][j] -= f * m[col][j]
    return [m[i][n] / m[i][i] for i in range(n)]


def design(row: dict) -> list[float]:
    return [1.0] + [row['inputs'][f] for f in mf.FEATURES]


class Normal:
    """Running X'X and X'y, so day i's fit uses exactly rows[:i]."""

    def __init__(self) -> None:
        n = len(mf.FEATURES) + 1
        self.xx = [[0.0] * n for _ in range(n)]
        self.xy = {'gap': [0.0] * n, 'c2c': [0.0] * n}
        self.count = 0

    def add(self, row: dict) -> None:
        x = design(row)
        for i, xi in enumerate(x):
            for j, xj in enumerate(x):
                self.xx[i][j] += xi * xj
            for key in self.xy:
                self.xy[key][i] += xi * row[key]
        self.count += 1

    def fit(self, key: str) -> list[float]:
        return solve(self.xx, self.xy[key])


def ols_direct(rows: list[dict], key: str) -> list[float]:
    """Independent refit from scratch, used only by the self-check."""
    xs = [design(r) for r in rows]
    n = len(xs[0])
    a = [[sum(x[i] * x[j] for x in xs) for j in range(n)] for i in range(n)]
    b = [sum(x[i] * r[key] for x, r in zip(xs, rows)) for i in range(n)]
    return solve(a, b)


def part(w: list[float]) -> dict[str, Any]:
    return {'intercept': w[0], 'coef': w[1:]}


# ---------------------------------------------------------------- walk-forward

def walk_forward(samples: list[dict]) -> list[dict]:
    normal = Normal()
    out = []
    for i, row in enumerate(samples):
        if i >= MIN_TRAIN_DAYS:
            wo, wc = normal.fit('gap'), normal.fit('c2c')
            row['wf'] = {'open': part(wo), 'close': part(wc), 'trainDays': normal.count,
                         'trainLast': samples[i - 1]['T']}
            out.append(row)
        normal.add(row)
    return out


def apply_thresholds(wf: list[dict], open_thr: float, close_thr: float) -> None:
    for row in wf:
        model = {'features': list(mf.FEATURES),
                 'open': {**row['wf']['open'], 'callThreshold': open_thr, 'strongThreshold': STRONG_THRESHOLD},
                 'close': {**row['wf']['close'], 'callThreshold': close_thr}, 'analogBuckets': []}
        row['pred'] = mf.predict(row['inputs'], model)


def same_dir(call: str, actual: float) -> bool:
    """Exactly 0 actual is a miss (SPEC §6.3)."""
    return (call == 'up' and actual > 0) or (call == 'down' and actual < 0)


def sign_call(value: float) -> str:
    return 'up' if value > 0 else 'down' if value < 0 else 'none'


def threshold_table(wf: list[dict]) -> list[dict]:
    apply_thresholds(wf, 0.0, 0.0)
    table = []
    for k in range(0, 31):
        thr = round(k * 0.1, 1)
        o = [r for r in wf if abs(r['pred']['openPct']) >= thr and r['pred']['openPct'] != 0]
        c = [r for r in wf if abs(r['pred']['closePct']) >= thr and r['pred']['closePct'] != 0]
        oh = sum(same_dir(sign_call(r['pred']['openPct']), r['gap']) for r in o)
        ch = sum(same_dir(sign_call(r['pred']['closePct']), r['c2c']) for r in c)
        years: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in o:
            years[r['year']][0] += same_dir(sign_call(r['pred']['openPct']), r['gap'])
            years[r['year']][1] += 1
        table.append({'thr': thr, 'openDays': len(o), 'openHits': oh, 'openHitPct': pct(oh, len(o)),
                      'openYears': {y: tuple(v) for y, v in sorted(years.items())},
                      'closeDays': len(c), 'closeHits': ch, 'closeHitPct': pct(ch, len(c))})
    return table


def pick_threshold(table: list[dict], key: str, target: float, by_year: bool = False) -> float | None:
    """Smallest threshold (0.1 steps) whose hit rate reaches target; with by_year, also every
    year with at least MIN_YEAR_CALLS calls (SPEC §6.2, revised 2026-11-06)."""
    for row in table:
        if row['thr'] < 0.1 or row[key] is None or row[key] < target:
            continue
        if by_year and any(n >= MIN_YEAR_CALLS and 100 * h / n < target for h, n in row['openYears'].values()):
            continue
        return row['thr']
    return None


def corr(a: list[float], b: list[float]) -> float:
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else 0.0


def rank_corr(a: list[float], b: list[float]) -> float:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        k = 0
        while k < len(order):
            j = k
            while j + 1 < len(order) and v[order[j + 1]] == v[order[k]]:
                j += 1
            for m in range(k, j + 1):
                out[order[m]] = (k + j) / 2
            k = j + 1
        return out
    return corr(ranks(a), ranks(b))


def pct(a: int, b: int) -> float | None:
    return round(100 * a / b, 1) if b else None


def quant(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method='inclusive')[max(0, min(98, round(q * 100) - 1))]


# ---------------------------------------------------------------- model numbers

def bucket_edges(open_thr: float) -> list[float]:
    return sorted({round(open_thr, 1)} | {e for e in (0.6, STRONG_THRESHOLD) if e > open_thr})


def fmt(x: float) -> str:
    return f'{x:.1f}'


def analog_buckets(wf: list[dict], open_thr: float) -> list[dict]:
    edges = bucket_edges(open_thr)
    buckets = []
    # down side, strongest first: (-inf, -e_last], (-e_last, -e_prev], ...
    neg = [-e for e in reversed(edges)]
    for i, hi in enumerate(neg):
        lo = neg[i - 1] if i else None
        bid = f'open_le_{fmt(hi)}' if lo is None else f'open_{fmt(lo)}_{fmt(hi)}'
        rows = [r for r in wf if (lo is None or r['pred']['openPct'] > lo) and r['pred']['openPct'] <= hi]
        buckets.append({'id': bid, 'lo': lo, 'hi': hi, 'days': len(rows),
                        'openDown': sum(r['gap'] < 0 for r in rows), 'closeDown': sum(r['c2c'] < 0 for r in rows)})
    for i, lo in enumerate(edges):
        hi = edges[i + 1] if i + 1 < len(edges) else None
        bid = f'open_ge_{fmt(lo)}' if hi is None else f'open_{fmt(lo)}_{fmt(hi)}'
        rows = [r for r in wf if r['pred']['openPct'] >= lo and (hi is None or r['pred']['openPct'] < hi)]
        buckets.append({'id': bid, 'lo': lo, 'hi': hi, 'days': len(rows),
                        'openUp': sum(r['gap'] > 0 for r in rows), 'closeUp': sum(r['c2c'] > 0 for r in rows)})
    return buckets


def open_bands(wf: list[dict], open_thr: float) -> list[dict]:
    bands = []
    recent_from = (date.fromisoformat(wf[-1]['T']) - timedelta(days=365)).isoformat() if wf else ''
    for strength, max_abs in (('weak', open_thr), ('moderate', STRONG_THRESHOLD), ('strong', None)):
        rows = [r for r in wf if r['pred']['strength'] == strength]
        errs = [abs(r['gap'] - r['pred']['openPct']) for r in rows]
        # p80AbsErr (plan S7): the range shown on the card holds 8 of 10 past opens, not half of them.
        p80 = round(statistics.quantiles(errs, n=10, method='inclusive')[7], 2) if len(errs) >= 10 else None
        # The card says how often that range held in the last year, which is the honest number when the market got
        # rougher than the long history (11-10: 54-75% instead of 80%).
        recent = [abs(r['gap'] - r['pred']['openPct']) for r in rows if r['T'] >= recent_from]
        cover = round(sum(e <= p80 for e in recent) / len(recent), 2) if p80 is not None and len(recent) >= 20 else None
        bands.append({'maxAbs': max_abs, 'p50AbsErr': round(statistics.median(errs), 2) if errs else None,
                      'p80AbsErr': p80, 'p80RecentCoverage': cover, 'recentDays': len(recent), 'days': len(errs)})
    return bands


def base_rates(wf: list[dict]) -> dict[str, Any]:
    down = [r for r in wf if r['gap'] <= -GAP_BIG]
    up = [r for r in wf if r['gap'] >= GAP_BIG]
    mod = [r for r in wf if r['pred']['strength'] == 'moderate']
    strong = [r for r in wf if r['pred']['strength'] == 'strong']
    weak = [r for r in wf if r['pred']['strength'] == 'weak']
    calls = mod + strong
    weak_close = [r for r in weak if r['pred']['closePct'] != 0]

    def hit_rate(rows: list[dict], actual: str, call_of=lambda r: r['pred']['openCall']) -> int | None:
        v = pct(sum(same_dir(call_of(r), r[actual]) for r in rows), len(rows))
        return None if v is None else round(v)

    return {
        'gapDownIntradayRebound': {'gapAtMost': -GAP_BIG, 'days': len(down), 'rebounded': sum(r['o2c'] > 0 for r in down)},
        'gapUpIntradayExtend': {'gapAtLeast': GAP_BIG, 'days': len(up), 'extended': sum(r['o2c'] > 0 for r in up)},
        'moderateOpenHitPct': hit_rate(mod, 'gap'),
        'moderateCloseHitPct': hit_rate(mod, 'c2c'),
        'strongOpenHitPct': hit_rate(strong, 'gap'),
        'strongCloseHitPct': hit_rate(strong, 'c2c'),
        'weakCloseHitPct': hit_rate(weak_close, 'c2c', lambda r: sign_call(r['pred']['closePct'])),
        'intradayHitPct': hit_rate(calls, 'o2c'),
        'definitions': {
            'moderateCloseHitPct': 'moderate days: close-to-previous-close sign equals the open call',
            'weakCloseHitPct': 'weak days: close-to-previous-close sign equals the sign of the close forecast',
            'intradayHitPct': 'call days: open-to-close sign equals the open call',
        },
    }


# ---------------------------------------------------------------- self-checks (no future data)

def hand_inputs(T: str, P: str, data: dict[str, Any]) -> dict[str, float | None]:
    """Independent recomputation from raw Yahoo bars and Naver rows (no shared helpers)."""
    raw = {s: data['yahooRaw'][s] for s in ('SPY', 'SOXX', 'EWY')}
    spy_days = sorted(d for d, (_, c) in raw['SPY'].items())
    sess = [d for d in spy_days if P <= d < T]
    if not sess:
        return {}
    base = spy_days[spy_days.index(sess[0]) - 1]
    out = {}
    for key, s in (('spyPct', 'SPY'), ('soxxPct', 'SOXX'), ('ewyPct', 'EWY')):
        out[key] = (raw[s][sess[-1]][1] / raw[s][base][1] - 1) * 100
    naver_days = sorted(data['naver'])
    inside = [d for d in naver_days if base < d <= P]
    if inside:
        before = naver_days[naver_days.index(inside[0]) - 1]
        out['kospiPrevPct'] = (data['naver'][P]['close'] / data['naver'][before]['close'] - 1) * 100
    else:
        out['kospiPrevPct'] = 0.0
    out['ewyExcessPct'] = out['ewyPct'] - out['kospiPrevPct']
    out['usSessions'] = sess
    return out


def self_checks(samples: list[dict], wf: list[dict], data: dict[str, Any]) -> list[dict]:
    checks = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})

    # 1. every US session used is dated P <= d < T, and its base bar is before the first session
    bad = [r['T'] for r in samples if not all(r['P'] <= d < r['T'] for d in r['usSessions'])
           or not r['usBase'] < r['usSessions'][0]]
    record('미국 세션 날짜가 P 이상 T 미만', not bad, f'{len(samples)}일 중 어긋남 {len(bad)}일 {bad[:5]}')
    # 2. every US session closed before T 09:00 KST
    late = []
    for r in samples:
        t_open = datetime.combine(date.fromisoformat(r['T']), dtime(9), tzinfo=KST_ZONE)
        for d in r['usSessions']:
            noon = datetime.combine(date.fromisoformat(d), dtime(12), tzinfo=NY_ZONE)
            close = datetime.combine(noon.date(), us_equity_close_time(noon), tzinfo=NY_ZONE)
            if close >= t_open:
                late.append((r['T'], d))
    record('미국 세션 마감이 T의 한국 09:00 전', not late, f'어긋남 {len(late)}건 {late[:3]}')
    # 3. no bar after the last completed session at fetch time
    after = {s: [d for d in data['us'][s].dates if d > (data['lastKr'] if s == '^KS11' else data['lastUs'])]
             for s in data['us']}
    record('받은 시각에 끝나지 않은 일봉 없음', not any(after.values()),
           f"미국 마지막 완료 세션 {data['lastUs']}, 한국 {data['lastKr']}")
    # 4. walk-forward fits use only earlier days and at least MIN_TRAIN_DAYS of them
    leak = [r['T'] for r in wf if not (r['wf']['trainLast'] < r['T'] and r['wf']['trainDays'] >= MIN_TRAIN_DAYS)]
    record('하루씩 앞으로: 학습 자료가 모두 예측일 전', not leak,
           f'{len(wf)}일 중 어긋남 {len(leak)}일, 최소 학습일 {min(r["wf"]["trainDays"] for r in wf)}')
    # 5. incremental fit equals a from-scratch fit on rows[:i] for random days
    rng = random.Random(20261106)
    index = {r['T']: i for i, r in enumerate(samples)}
    picks = rng.sample(wf, 6) + [wf[0], wf[-1]]
    worst = 0.0
    for r in picks:
        i = index[r['T']]
        for key, name in (('gap', 'open'), ('c2c', 'close')):
            direct = ols_direct(samples[:i], key)
            inc = [r['wf'][name]['intercept']] + list(r['wf'][name]['coef'])
            worst = max(worst, max(abs(a - b) for a, b in zip(direct, inc)))
    record('누적 합 학습 = 처음부터 다시 푼 학습(표본 8일)', worst < 1e-8, f'최대 차이 {worst:.2e}')
    # 6. inputs equal an independent recomputation from raw bars, for random days and the 11-06 case
    picks = (rng.sample(samples, 12) + rng.sample([r for r in samples if r['oldRuleDiffers']], 4)
             + [r for r in samples if r['T'] == CASE_20261106['T']])
    worst, mism = 0.0, []
    for r in picks:
        h = hand_inputs(r['T'], r['P'], data)
        if h.get('usSessions') != r['usSessions']:
            mism.append(r['T'])
            continue
        for key in ('spyPct', 'soxxPct', 'ewyPct', 'kospiPrevPct', 'ewyExcessPct'):
            worst = max(worst, abs(h[key] - r['inputs'][key]))
    record(f'입력 손계산 대조(표본 {len(picks)}일, 미국만 쉰 날 다음 4일 포함)', not mism and worst <= 0.011,
           f'최대 차이 {worst:.4f}%p (반올림 0.01 이내), 세션 어긋남 {mism}')
    # 7. 2026-11-06 reference case
    case = next((r for r in samples if r['T'] == CASE_20261106['T']), None)
    if case:
        diffs = {k: round(case['inputs'][k] - CASE_20261106[k], 2) for k in ('ewyExcessPct', 'spyPct', 'soxxPct')}
        n = data['naver']
        gap = (n[case['T']]['open'] / n[case['P']]['close'] - 1) * 100
        ok = all(abs(v) <= 0.02 for v in diffs.values()) and abs(gap - CASE_20261106['gapPct']) < 0.005 \
            and abs(n[case['T']]['open'] - CASE_20261106['open']) < 0.005 \
            and abs(n[case['P']]['close'] - CASE_20261106['prevClose']) < 0.005
        record('2026-11-06 사례', ok, f"입력 {case['inputs']} · 예상과 차이 {diffs} · 실제 시가 {gap:+.2f}% "
               f"(시가 {n[case['T']]['open']:,.2f}, 전일 종가 {n[case['P']]['close']:,.2f})")
    else:
        record('2026-11-06 사례', False, '표본에 없음')
    # 8. placebo: a forecast that saw nothing of session T cannot track T's intraday move (open->close).
    #    Positive control: EWY on the US session dated T (which trades after KR T closes) must track it.
    ewy = data['us']['EWY']
    pred = [r['pred']['openPct'] for r in wf]
    # Rank correlation: a few record days (2026-07-31 opened +1.2% and closed +17.9%) dominate Pearson's r.
    r_pred = rank_corr(pred, [r['o2c'] for r in wf])
    r_pearson = corr(pred, [r['o2c'] for r in wf])
    ctrl = [(ewy.ret(ewy.dates.index(r['T'])), r['o2c']) for r in wf if r['T'] in set(ewy.dates)]
    ctrl = [(a, b) for a, b in ctrl if a is not None]
    r_ctrl = rank_corr([a for a, _ in ctrl], [b for _, b in ctrl])
    record('장중 움직임과 상관 없음(미래 자료가 섞였으면 생김)', abs(r_pred) < 0.1 and r_ctrl > 0.3,
           f'순위 상관: 예상 시가 vs T일 장중 {r_pred:+.3f} ({len(wf)}일; 참고 Pearson {r_pearson:+.3f}, 큰 날 몇 개 때문) · '
           f'대조군: T일 미국장 EWY vs T일 장중 {r_ctrl:+.3f} ({len(ctrl)}일)')
    return checks


# ---------------------------------------------------------------- summaries

def summarize(wf: list[dict], open_thr: float, close_thr: float) -> dict[str, Any]:
    calls = [r for r in wf if r['pred']['openCall'] != 'none']
    close_calls = [r for r in wf if r['pred']['closeCall'] != 'none']
    years = defaultdict(lambda: {'days': 0, 'calls': 0, 'openHits': 0, 'closeCalls': 0, 'closeHits': 0,
                                 'intradayHits': 0})
    for r in wf:
        y = years[r['year']]
        y['days'] += 1
        if r['pred']['openCall'] != 'none':
            y['calls'] += 1
            y['openHits'] += same_dir(r['pred']['openCall'], r['gap'])
            y['intradayHits'] += same_dir(r['pred']['openCall'], r['o2c'])
        if r['pred']['closeCall'] != 'none':
            y['closeCalls'] += 1
            y['closeHits'] += same_dir(r['pred']['closeCall'], r['c2c'])
    return {
        'days': len(wf), 'calls': len(calls), 'openHits': sum(same_dir(r['pred']['openCall'], r['gap']) for r in calls),
        'noCallDays': len(wf) - len(calls), 'closeCalls': len(close_calls),
        'closeHits': sum(same_dir(r['pred']['closeCall'], r['c2c']) for r in close_calls),
        'years': dict(sorted(years.items())),
    }


def strength_table(wf: list[dict]) -> list[dict]:
    edges = [0, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 3.0, None]
    rows = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [r for r in wf if abs(r['pred']['openPct']) >= lo and (hi is None or abs(r['pred']['openPct']) < hi)]
        nz = [r for r in sel if r['pred']['openPct'] != 0]
        cz = [r for r in sel if r['pred']['closePct'] != 0]
        rows.append({'lo': lo, 'hi': hi, 'days': len(sel),
                     'openHitPct': pct(sum(same_dir(sign_call(r['pred']['openPct']), r['gap']) for r in nz), len(nz)),
                     'closeByOpenPct': pct(sum(same_dir(sign_call(r['pred']['openPct']), r['c2c']) for r in nz), len(nz)),
                     'closeByClosePct': pct(sum(same_dir(sign_call(r['pred']['closePct']), r['c2c']) for r in cz), len(cz)),
                     'intradayPct': pct(sum(same_dir(sign_call(r['pred']['openPct']), r['o2c']) for r in nz), len(nz)),
                     'p50AbsErr': round(statistics.median([abs(r['gap'] - r['pred']['openPct']) for r in sel]), 2) if sel else None})
    return rows


def window_stats(rows: list[dict], open_levels=(0.0, 0.3, 0.6, 1.0), close_levels=(0.0, 0.3, 0.6, 1.0)) -> dict:
    out = {}
    for thr in open_levels:
        s = [r for r in rows if abs(r['pred']['openPct']) >= thr and r['pred']['openPct'] != 0]
        out[f'open>={thr}'] = (sum(same_dir(sign_call(r['pred']['openPct']), r['gap']) for r in s), len(s))
    for thr in close_levels:
        s = [r for r in rows if abs(r['pred']['closePct']) >= thr and r['pred']['closePct'] != 0]
        out[f'close>={thr}'] = (sum(same_dir(sign_call(r['pred']['closePct']), r['c2c']) for r in s), len(s))
    return out


def yahoo_open_effect(wf: list[dict], data: dict[str, Any]) -> dict[str, Any]:
    ks = data['yahooRaw']['^KS11']
    days = sorted(ks)
    flat, flips, compared = 0, 0, 0
    for r in wf:
        if r['T'] not in ks or r['P'] not in ks or r['pred']['openCall'] == 'none':
            continue
        o, _ = ks[r['T']]
        _, pc = ks[r['P']]
        if not o or not pc:
            continue
        compared += 1
        ygap = (o / pc - 1) * 100
        if abs(ygap) < 0.01:
            flat += 1
        if same_dir(r['pred']['openCall'], ygap) != same_dir(r['pred']['openCall'], r['gap']):
            flips += 1
    all_flat, naver_flat_too = 0, 0
    naver = data['naver']
    for i in range(1, len(days)):
        o, _ = ks[days[i]]
        _, pc = ks[days[i - 1]]
        if o and pc and abs(o / pc - 1) < 0.0001:
            all_flat += 1
            no, npc = (naver.get(days[i]) or {}).get('open'), (naver.get(days[i - 1]) or {}).get('close')
            naver_flat_too += bool(no and npc and abs(no / npc - 1) < 0.0001)
    open_diff = sum(1 for d in days if d in naver and ks[d][0] and naver[d]['open']
                    and abs(ks[d][0] / naver[d]['open'] - 1) > 0.002)
    return {'compared': compared, 'yahooFlatOnCallDays': flat, 'hitDiffers': flips, 'yahooFlatAll': all_flat,
            'naverFlatToo': naver_flat_too, 'openDiffOver02': open_diff}


# ---------------------------------------------------------------- report

def p(x: float | None, digits: int = 1) -> str:
    return '–' if x is None else f'{x:.{digits}f}'


def sgn(x: float | None, digits: int = 2) -> str:
    if x is None:
        return '–'
    text = f'{x:+.{digits}f}'
    return text.replace('-', '−')


def frac(h: int, n: int) -> str:
    return f'{h}/{n} ({p(pct(h, n))}%)' if n else '0/0'


def write_report(path: Path, ctx: dict[str, Any]) -> None:
    m, s, wf, data = ctx['model'], ctx['summary'], ctx['wf'], ctx['data']
    L: list[str] = []
    add = L.append
    add('# 꽁치의 아침 예보 1-A: 학습·하루씩 앞으로 가는 시험 보고서')
    add('')
    add(f"- 만든 때: {ctx['generatedAt']} · 자료 받은 때: {data['meta']['fetchedAt']} (UTC)")
    add(f"- 스크립트: `tools/polarmeter_forecast_train.py` · 입력 함수: `tools/polarmeter_morning_forecast.py` (실시간 계층과 같은 함수)")
    add(f"- 모델: `tools/polarmeter_forecast_model.json` (`{m['modelVersion']}`, 학습 끝 날 {m['trainedThrough']})")
    add('- 검수: 자체 검수 (확인한 것·못 한 것은 맨 끝)')
    add('')
    stop = ctx['stop']
    add('## 0. 요약')
    add('')
    add(f"- 하루씩 앞으로 가는 시험 기간 {m['walkForward']['from']} ~ {m['walkForward']['to']}, {s['days']}거래일 "
        f"(각 날을 그 전날까지 {MIN_TRAIN_DAYS}일 이상으로 만든 식으로 예측).")
    add(f"- 시가 방향 기준 **{m['open']['callThreshold']}%**, 종가 방향 기준 **{m['close']['callThreshold']}%** "
        f"(0.1 단위로 가장 작은 값. 시가: 방향 낸 날 전체 {OPEN_TARGET_HIT:.0f}% 이상이고 방향 낸 날이 {MIN_YEAR_CALLS}일 이상인 모든 해에서도 "
        f"{OPEN_TARGET_HIT:.0f}% 이상. 종가: 전체 {CLOSE_TARGET_HIT:.0f}% 이상. 3절 표).")
    add(ctx['thresholdNote'])
    add(f"- 방향 낸 날 {s['calls']}일 중 시가 적중 {s['openHits']}일 (**{p(pct(s['openHits'], s['calls']))}%**). "
        f"방향 안 낸 날 {s['noCallDays']}일. 종가 방향 낸 날 {s['closeCalls']}일 중 {s['closeHits']}일 적중 ({p(pct(s['closeHits'], s['closeCalls']))}%).")
    add(f"- 멈춤 조건(전체 {STOP_OVERALL_OPEN_HIT:.0f}% 미만, 방향 낸 날 {MIN_YEAR_CALLS}일 이상인 해 중 {STOP_YEAR_OPEN_HIT:.0f}% 미만): "
        + ('**해당함** — ' + '; '.join(stop) if stop else '**해당 없음**') + '.'
        + (' 판정에서 뺀 해(방향 낸 날 ' + str(MIN_YEAR_CALLS) + '일 미만, 숫자만 적음): ' + '; '.join(ctx['smallYears']) if ctx['smallYears'] else ''))
    add(f"- 한국 ETF 초과분(§6.3, 2026-11-06 고침): 코스피는 EWY 기준 일봉 뒤 ~ P의 누적 등락률을 뺀다. 9절에 고치기 전과 비교.")
    add('')
    add('## 1. 자료와 표본')
    add('')
    cc = ctx['check']
    add(f"- 코스피 시가·종가: 네이버 일별 시세 {len(data['naver'])}줄 ({min(data['naver'])} ~ {max(data['naver'])}).")
    add(f"- 야후 `^KS11` 종가 대조: {cc['checked']}일 대조, 0.3% 넘게 다른 날 **{len(cc['bad'])}일** "
        f"(최대 차이 {p(cc['maxAbsDiffPct'], 3)}%). 야후에 없어 대조 못 한 날 {len(cc['unverified'])}일(뺀 날 아님, 네이버 값 사용). "
        f"야후에만 있고 네이버에 없는 날 {len(cc['yahooOnly'])}일.")
    if cc['bad']:
        add('')
        add('  | 날짜 | 차이 |')
        add('  |---|---|')
        for d, why in sorted(cc['bad'].items()):
            add(f'  | {d} | {why} |')
    if cc['yahooOnly']:
        add(f"  - 야후에만 있는 날: {', '.join(cc['yahooOnly'][:20])}")
    add(f"- 미국: 야후 일봉 SPY·SOXX·EWY, 거래소 현지 날짜. 받은 때 기준 끝난 마지막 미국 세션 {data['lastUs']}, 한국 {data['lastKr']}. 그 뒤 일봉은 버림.")
    add(f"- 표본(예보를 낼 수 있었던 한국 거래일): **{len(ctx['samples'])}일** ({ctx['samples'][0]['T']} ~ {ctx['samples'][-1]['T']}).")
    add('- 표본에서 빠진 날:')
    names = {'us_market_closed': '밤사이 미국 휴장(예보 없음)', 'kr_price_mismatch': '네이버·야후 종가 0.3% 넘게 다름(T·P·P 전날 중 하나)',
             'kr_open_missing': '네이버 시가·종가 없음', 'us_session_mismatch': '미국 세 종목 날짜 어긋남',
             'input_missing': '미국 종가 빔', 'kospi_prev_missing': '코스피 전날 종가 빔', 'us_series_missing': '미국 자료 없음'}
    for reason, n in ctx['skipped'].most_common():
        days = ctx['skippedDays'][reason]
        add(f"  - {names.get(reason, reason)}: {n}일 ({', '.join(days[:12])}{' …' if len(days) > 12 else ''})")
    flat = sum(r['openEqPrev'] for r in ctx['samples'])
    add(f"- 네이버 시가가 전일 종가와 0.01% 안으로 같은 날: {flat}일. 예비 시험에서 야후 시가 이상으로 의심한 14일은 네이버로 봐도 보합 출발이었다(8절).")
    sus = [r['T'] for r in ctx['samples'] if any(abs(r['inputs'][k]) > SUSPECT_INPUT_ABS for k in ('spyPct', 'soxxPct', 'ewyPct'))]
    add(f"- 입력 등락률 절대값이 {SUSPECT_INPUT_ABS:.0f}%를 넘는 날(1-B에서 `inputs_suspect`로 예보를 안 낼 날): {len(sus)}일 {sus}. 학습에는 넣었다.")
    add('')
    add('## 2. 미래 자료가 섞이지 않았는지 (스크립트 자체 점검)')
    add('')
    add('| 점검 | 결과 | 내용 |')
    add('|---|---|---|')
    for c in ctx['checks']:
        add(f"| {c['name']} | {'통과' if c['ok'] else '**실패**'} | {c['detail']} |")
    add('')
    add('## 3. 기준 값 고르기 (하루씩 앞으로 가는 예측, |예상| ≥ 기준인 날 전부)')
    add('')
    years = sorted({y for t in ctx['thresholds'] for y in t['openYears']})
    add('| 기준 | 시가: 낸 날 | 시가 적중 | ' + ' | '.join(f'시가 {y}' for y in years) + ' | 종가: 낸 날 | 종가 적중 |')
    add('|---|---|---|' + '---|' * len(years) + '---|---|')
    for t in ctx['thresholds']:
        if t['thr'] <= 2.0:
            mark_o = ' ◀' if t['thr'] == m['open']['callThreshold'] else ''
            mark_c = ' ◀' if t['thr'] == m['close']['callThreshold'] else ''
            cells = []
            for y in years:
                h, n = t['openYears'].get(y, (0, 0))
                text = f'{p(pct(h, n))}% ({n})' if n else '–'
                cells.append(text if n >= MIN_YEAR_CALLS or not n else f'({text})')
            add(f"| {t['thr']:.1f}% | {t['openDays']} | {p(t['openHitPct'])}%{mark_o} | " + ' | '.join(cells)
                + f" | {t['closeDays']} | {p(t['closeHitPct'])}%{mark_c} |")
    add('')
    add(f'해마다 괄호 안은 방향 낸 날 수. 방향 낸 날이 {MIN_YEAR_CALLS}일 미만인 해는 전체를 괄호로 묶었고 기준 고르기에 쓰지 않았다.')
    add('')
    add('## 4. 예상 세기 구간별 적중 (|예상 시가| 구간)')
    add('')
    add('| 구간 | 일수 | 시가 방향 | 종가 방향(시가 예보 부호) | 종가 방향(종가 예보 부호) | 장중 방향 | 시가 오차 중앙값 |')
    add('|---|---|---|---|---|---|---|')
    for r in ctx['strength']:
        hi = '∞' if r['hi'] is None else f"{r['hi']:.1f}"
        add(f"| {r['lo']:.1f} ~ {hi}% | {r['days']} | {p(r['openHitPct'])}% | {p(r['closeByOpenPct'])}% | "
            f"{p(r['closeByClosePct'])}% | {p(r['intradayPct'])}% | {p(r['p50AbsErr'], 2)}%p |")
    add('')
    add('과거 기록표(`analogBuckets`, 시험 기간 전체):')
    add('')
    add('| 구간 | 일수 | 같은 방향 출발 | 같은 방향 마감 |')
    add('|---|---|---|---|')
    for b in m['analogBuckets']:
        o = b.get('openDown', b.get('openUp'))
        c = b.get('closeDown', b.get('closeUp'))
        add(f"| {b['id']} | {b['days']} | {frac(o, b['days'])} | {frac(c, b['days'])} |")
    add('')
    add('## 5. 연도별 적중')
    add('')
    add('| 해 | 거래일 | 시가 방향 낸 날 | 시가 적중 | 종가 방향 낸 날 | 종가 적중 | 장중(낸 날) |')
    add('|---|---|---|---|---|---|---|')
    for y, v in s['years'].items():
        y = y if v['calls'] >= MIN_YEAR_CALLS else f'{y} (판정 제외: 방향 낸 날 {MIN_YEAR_CALLS}일 미만)'
        add(f"| {y} | {v['days']} | {v['calls']} | {frac(v['openHits'], v['calls'])} | {v['closeCalls']} | "
            f"{frac(v['closeHits'], v['closeCalls'])} | {frac(v['intradayHits'], v['calls'])} |")
    add('')
    add('## 6. 장중(시가→종가)과 큰 갭 뒤')
    add('')
    br = m['baseRates']
    calls = [r for r in wf if r['pred']['openCall'] != 'none']
    add(f"- 장중 방향: 방향 낸 날 {len(calls)}일 중 시가 예보 방향으로 더 간 날 {frac(sum(same_dir(r['pred']['openCall'], r['o2c']) for r in calls), len(calls))}. "
        f"(`intradayHitPct` {br['intradayHitPct']}%)")
    nz = [r for r in wf if round(r['pred']['closePct'] - r['pred']['openPct'], 2) != 0]
    add(f"- 참고: 예상 종가−예상 시가 부호로 장중 방향을 맞힌 비율 {frac(sum(same_dir(sign_call(r['pred']['closePct'] - r['pred']['openPct']), r['o2c']) for r in nz), len(nz))}.")
    g = br['gapDownIntradayRebound']
    add(f"- 실제 시가 −{GAP_BIG}% 이하 출발 {g['days']}일 중 장중 반등(종가>시가) {frac(g['rebounded'], g['days'])}.")
    g = br['gapUpIntradayExtend']
    add(f"- 실제 시가 +{GAP_BIG}% 이상 출발 {g['days']}일 중 장중 추가 상승 {frac(g['extended'], g['days'])}.")
    add(f"- 보통 세기 날: 시가 {br['moderateOpenHitPct']}%, 같은 방향 마감 {br['moderateCloseHitPct']}%. 뚜렷한 날: 시가 {br['strongOpenHitPct']}%, 같은 방향 마감 {br['strongCloseHitPct']}%. "
        f"방향 안 낸 날의 종가 방향(종가 예보 부호) {br['weakCloseHitPct']}%.")
    add('')
    add('## 7. 시가 크기 오차 (실제 시가 − 예상 시가)')
    add('')
    add('| 구간 | 일수 | 절대 오차 25% | 50% | 75% | 90% | 평균 치우침 | 실제가 예상보다 더 크게 움직인 날 |')
    add('|---|---|---|---|---|---|---|---|')
    for label, sel in (('전체', wf),) + tuple((st, [r for r in wf if r['pred']['strength'] == st]) for st in ('weak', 'moderate', 'strong')):
        errs = [abs(r['gap'] - r['pred']['openPct']) for r in sel]
        bias = statistics.mean(r['gap'] - r['pred']['openPct'] for r in sel) if sel else None
        bigger = sum(abs(r['gap']) > abs(r['pred']['openPct']) for r in sel)
        add(f"| {label} | {len(sel)} | {p(quant(errs, .25), 2)} | {p(quant(errs, .5), 2)} | {p(quant(errs, .75), 2)} | "
            f"{p(quant(errs, .9), 2)} | {sgn(bias)}%p | {frac(bigger, len(sel))} |")
    add('')
    add('`openBands`(모델 파일): ' + ', '.join(f"|예상| < {b['maxAbs']}: {b['p50AbsErr']}%p ({b['days']}일)" if b['maxAbs'] is not None
                                          else f"그 이상: {b['p50AbsErr']}%p ({b['days']}일)" for b in m['openBands']))
    add('')
    add('## 8. 예비 시험(§2)과의 차이')
    add('')
    pr = ctx['prelimCompare']
    add(f"같은 기간({pr['from']} ~ {pr['to']}, 이번 표본 {pr['days']}일)만 떼어 본 하루씩 앞으로 가는 결과:")
    add('')
    add('| 맞히려는 것 | 전체 | ±0.3% 이상 | ±0.6% 이상 | ±1% 이상 |')
    add('|---|---|---|---|---|')
    w = pr['stats']
    add('| 시가 방향(이번) | ' + ' | '.join(frac(*w[f'open>={t}']) for t in (0.0, 0.3, 0.6, 1.0)) + ' |')
    add('| 시가 방향(예비) | 74.8% (472) | 84.4% (282) | 88.5% (156) | 91.5% (82) |')
    add('| 종가 방향(이번) | ' + ' | '.join(frac(*w[f'close>={t}']) for t in (0.0, 0.3, 0.6, 1.0)) + ' |')
    add('| 종가 방향(예비) | 61.4% | 70.9% (254) | 78.3% (129) | 81.4% (59) |')
    add('')
    fo = m['open']
    add(f"- 최종 계수(전체 자료): 시가 절편 {fo['intercept']}, 계수 {fo['coef']} (예비 {PRELIM['open']['intercept']}, {PRELIM['open']['coef']}); "
        f"종가 절편 {m['close']['intercept']}, 계수 {m['close']['coef']} (예비 {PRELIM['close']['intercept']}, {PRELIM['close']['coef']}). 순서 {list(mf.FEATURES)}.")
    ye = ctx['yahooOpen']
    add(f"- 시가 자료: 예비는 야후 시가, 이번은 네이버 공식 시가. 야후 `^KS11` 시가가 전일 종가와 같은 날이 전체 {ye['yahooFlatAll']}일, "
        f"그중 네이버로도 시가가 전일 종가와 0.01% 안인 날 {ye['naverFlatToo']}일(즉 야후 오류가 아니라 실제 보합 출발). "
        f"야후·네이버 시가가 0.2% 넘게 다른 날 {ye['openDiffOver02']}일. 같은 예측을 야후 시가로 채점하면 적중 여부가 바뀌는 날 {ye['hitDiffers']}일 / {ye['compared']}일. "
        "그래서 시가 자료 차이는 이번 결과에 거의 영향이 없다.")
    add('- 방식: 예비는 앞 60%로 한 번 만든 식을 뒤 40%에 썼다. 이번은 날마다 그 전날까지 자료로 식을 다시 만들었다(과거 자료가 점점 늘어남).')
    add('- 입력 반올림: 이번은 입력을 0.01%로, 예상을 0.01%로 반올림한 뒤 판정한다(실시간 공개 값과 같게).')
    add('')
    add('## 9. 한국 휴장·미국 휴장이 낀 날')
    add('')
    hol = ctx['holiday']
    for line in hol:
        add(f'- {line}')
    add('')
    add('## 10. 최근 20거래일 (하루씩 앞으로 가는 예측)')
    add('')
    add('| 날짜 | 입력(초과분/SPY/SOXX) | 예상 시가 | 판정 | 실제 시가 | 실제 종가 | 적중 |')
    add('|---|---|---|---|---|---|---|')
    for r in wf[-20:]:
        i = r['inputs']
        call = r['pred']['openCall']
        hit = '–' if call == 'none' else ('적중' if same_dir(call, r['gap']) else '빗나감')
        add(f"| {r['T']} | {sgn(i['ewyExcessPct'])} / {sgn(i['spyPct'])} / {sgn(i['soxxPct'])} | {sgn(r['pred']['openPct'])}% | "
            f"{r['pred']['strength']} {call} | {sgn(r['gap'])}% | {sgn(r['c2c'])}% | {hit} |")
    last20 = [r for r in wf[-20:] if r['pred']['openCall'] != 'none']
    add('')
    add(f"최근 20거래일: 방향 {len(last20)}번, 시가 적중 {sum(same_dir(r['pred']['openCall'], r['gap']) for r in last20)}번.")
    add('')
    add('## 11. 한계')
    add('')
    for line in ctx['limits']:
        add(f'- {line}')
    add('')
    path.write_text('\n'.join(L) + '\n', encoding='utf-8')


# ---------------------------------------------------------------- main

def old_rule_walk_forward(samples: list[dict], open_thr: float, close_thr: float) -> list[dict]:
    """The same walk-forward with the first design's ewyExcessPct, for comparison only."""
    clones = [{**r, 'inputs': {**r['inputs'], 'ewyExcessPct': r['oldEwyExcessPct']}} for r in samples]
    owf = walk_forward(clones)
    apply_thresholds(owf, open_thr, close_thr)
    return owf


def holiday_lines(samples: list[dict], wf: list[dict], skipped_days: dict[str, list], old_wf: list[dict],
                  open_thr: float, close_thr: float) -> list[str]:
    def hits(rows: list[dict]) -> str:
        c = [r for r in rows if r.get('pred') and r['pred']['openCall'] != 'none']
        return frac(sum(same_dir(r['pred']['openCall'], r['gap']) for r in c), len(c))
    wf_set = {r['T'] for r in wf}
    multi = [r for r in samples if len(r['usSessions']) > 1]
    no_p = [r for r in samples if r['P'] not in r['usSessions']]
    kr_hol = [r for r in samples if r['krHolidayGap']]
    changed = [r for r in samples if r['oldRuleDiffers']]
    two_kr = [r for r in samples if len(r['krInEwy']) > 1]
    new_changed = [r for r in wf if r['oldRuleDiffers']]
    old_changed = [r for r in old_wf if r['oldRuleDiffers']]
    old_calls = [r for r in old_wf if r['pred']['openCall'] != 'none']
    out = [
        f"밤사이 미국 휴장(미국 세션 0개): {len(skipped_days.get('us_market_closed', []))}일 → 예보 없음(`us_market_closed`). "
        f"{', '.join(skipped_days.get('us_market_closed', [])[:15])}",
        f"한국만 쉰 날 뒤(미국 세션 2개 이상 누적): {len(multi)}일, 그중 시험 기간 {sum(r['T'] in wf_set for r in multi)}일, 방향 낸 날 시가 적중 {hits([r for r in multi if r['T'] in wf_set])}. "
        f"예: {', '.join(r['T'] + '(' + '+'.join(r['usSessions']) + ')' for r in multi[-4:])}",
        f"P와 T 사이에 한국 휴장 평일이 낀 날: {len(kr_hol)}일, 시험 기간 방향 낸 날 적중 {hits([r for r in kr_hol if r['T'] in wf_set])}.",
        f"미국 세션에 P 날짜가 없는 날(P에 미국 휴장): {len(no_p)}일. {', '.join(r['T'] for r in no_p[:10])}",
        f"미국만 쉰 날 다음(EWY 기준 일봉 뒤 ~ P에 한국 거래일 2개 이상, 코스피 이틀치 이상을 뺌): {len(two_kr)}일. "
        f"예: {', '.join(r['T'] + '(코스피 ' + '+'.join(r['krInEwy']) + ')' for r in two_kr[-3:])}",
        f"**고치기 전과 비교.** 처음 설계(P 하루치만, P가 미국 세션일 때만 뺌)와 입력이 달라지는 날 {len(changed)}일. 같은 기준 값"
        f"(시가 {open_thr}% · 종가 {close_thr}%)으로 하루씩 앞으로 가는 시험을 두 번 돌린 결과, 그중 시험 기간 {len(new_changed)}일의 방향 낸 날 시가 적중: "
        f"고친 뒤 {hits(new_changed)} · 고치기 전 {hits(old_changed)}. "
        f"시험 기간 전체: 고친 뒤 {hits(wf)} · 고치기 전 {hits(old_wf)} (고치기 전 방향 낸 날 {len(old_calls)}일). "
        f"참고: 첫 실행(고치기 전, 기준 0.2)의 같은 날 적중은 13/19였다. {', '.join(r['T'] for r in changed[-8:])}",
    ]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--data-dir', required=True, type=Path)
    ap.add_argument('--report', required=True, type=Path)
    ap.add_argument('--model-out', type=Path, default=mf.MODEL_PATH)
    ap.add_argument('--fetch', action='store_true', help='download fresh raw data into --data-dir first')
    args = ap.parse_args(argv)
    if args.fetch:
        fetch_all(args.data_dir)
    data = load_data(args.data_dir)
    check = cross_check(data['naver'], data['us']['^KS11'])
    samples, skipped, skipped_days = build_samples(data, check)
    if len(samples) <= MIN_TRAIN_DAYS + 100:
        print('not enough samples', len(samples))
        return 2
    wf = walk_forward(samples)
    table = threshold_table(wf)
    open_thr = pick_threshold(table, 'openHitPct', OPEN_TARGET_HIT, by_year=True)
    close_thr = pick_threshold(table, 'closeHitPct', CLOSE_TARGET_HIT)
    if open_thr is None or close_thr is None:
        print('no threshold reaches the target', open_thr, close_thr)
        return 3
    apply_thresholds(wf, open_thr, close_thr)
    summary = summarize(wf, open_thr, close_thr)

    final = Normal()
    for row in samples:
        final.add(row)
    wo, wc = final.fit('gap'), final.fit('c2c')
    model = {
        'modelVersion': mf.MODEL_VERSION,
        'trainedThrough': samples[-1]['T'],
        'features': list(mf.FEATURES),
        'open': {'intercept': round(wo[0], 4), 'coef': [round(x, 4) for x in wo[1:]],
                 'callThreshold': open_thr, 'strongThreshold': STRONG_THRESHOLD},
        'close': {'intercept': round(wc[0], 4), 'coef': [round(x, 4) for x in wc[1:]], 'callThreshold': close_thr},
        'openBands': open_bands(wf, open_thr),
        'analogBuckets': analog_buckets(wf, open_thr),
        'baseRates': base_rates(wf),
        'walkForward': {'from': wf[0]['T'], 'to': wf[-1]['T'], 'days': summary['days'], 'calls': summary['calls'],
                        'openHits': summary['openHits'], 'noCallDays': summary['noCallDays'],
                        'closeCalls': summary['closeCalls'], 'closeHits': summary['closeHits'],
                        'minTrainDays': MIN_TRAIN_DAYS},
        'trainingSamples': {'from': samples[0]['T'], 'to': samples[-1]['T'], 'days': len(samples)},
        'dataSources': {'krIndex': 'naver_index_price', 'us': 'yahoo_chart_daily'},
    }
    checks = self_checks(samples, wf, data)
    stop = []
    overall = pct(summary['openHits'], summary['calls'])
    if overall is None or overall < STOP_OVERALL_OPEN_HIT:
        stop.append(f'전체 시가 적중 {overall}%')
    for y, v in summary['years'].items():
        r = pct(v['openHits'], v['calls'])
        if r is not None and r < STOP_YEAR_OPEN_HIT and v['calls'] >= MIN_YEAR_CALLS:
            span = [w['T'] for w in wf if w['year'] == y]
            stop.append(f'{y}년 시가 적중 {r}% ({v["openHits"]}/{v["calls"]}, 시험 기간 안 {v["days"]}거래일 {span[0]}~{span[-1]})')
    small_years = [f"{y}년 {frac(v['openHits'], v['calls'])}" for y, v in summary['years'].items() if v['calls'] < MIN_YEAR_CALLS]
    old_wf = old_rule_walk_forward(samples, open_thr, close_thr)
    row = next(t for t in table if t['thr'] == open_thr)
    lower = [t for t in table if 0.1 <= t['thr'] < open_thr]
    why = '; '.join(
        f"{t['thr']:.1f}%: 전체 {p(t['openHitPct'])}%" + ''.join(
            f", {y}년 {p(pct(h, n))}% ({h}/{n})" for y, (h, n) in t['openYears'].items()
            if n >= MIN_YEAR_CALLS and 100 * h / n < OPEN_TARGET_HIT) for t in lower)
    threshold_note = (f"- 시가 기준 {open_thr}%: 전체 {p(row['openHitPct'])}%, "
                      + ', '.join(f"{y}년 {p(pct(h, n))}% ({h}/{n})" for y, (h, n) in row['openYears'].items())
                      + f". 그보다 작은 값이 탈락한 이유 — {why}."
                      + ('' if open_thr == 0.3 else f" 폴실장 예상(0.3)과 다르다. 규칙대로 나온 값을 썼다."))
    prelim_rows = [r for r in wf if '2024-11-06' <= r['T'] <= '2026-11-06']
    limits = [
        '기준 값은 하루씩 앞으로 가는 예측 결과를 보고 골랐다. 그래서 기준을 고른 그 기간의 적중률은 기준 고르기에 대해서는 표본 안 숫자다(계수는 표본 밖).',
        f"시험 기간은 {wf[0]['T']}부터다. 처음 {MIN_TRAIN_DAYS}일은 학습 전용이라 그 기간(주로 2022~2023)의 성적은 없다. 과거 기록 문구의 기간은 모델의 `walkForward.from`({wf[0]['T']})에서 만든다(§4.4, 약 {round((date.fromisoformat(wf[-1]['T']) - date.fromisoformat(wf[0]['T'])).days / 365.25, 1)}년).",
        f'연도별 표에서 첫 해와 마지막 해는 일부 기간만이다. 방향 낸 날이 {MIN_YEAR_CALLS}일 미만인 해는 기준 고르기와 멈춤 판정에서 뺐다.',
        '고치기 전 입력과의 비교(9절)는 같은 기준 값으로 시험을 한 번 더 돌린 것이다. 기준 값은 고친 입력으로만 골랐다.',
        '종가·장중은 밤사이 미국장만으로는 잘 맞지 않는다. 장중 방향은 반반 수준이라 예보하지 않는다(§3-5).',
        '크기: 실제 시가가 예상보다 크게 움직이는 경향(위 7절). 크기는 "안팎"과 범위로만 말해야 한다.',
        '대조: 네이버 종가는 야후 종가와만 대조했다. 네이버 시가는 대조할 공식 자료가 없다(야후 시가는 이상이 있어 대조로 쓰지 않음).',
        '2021~2025년 한국 휴장일은 저장소 달력에 없어 네이버 거래일 목록을 그대로 믿었다. 야후 ^KS11 거래일과의 차이는 1절에 적었다.',
        '미국 조기 폐장(13:00)은 2026~2028 공표분만 달력에 있다. 그 전 해의 조기 폐장은 16:00으로 보고 점검했다(마감 시각을 늦게 보는 쪽이라 점검이 더 엄격함).',
        '매매 수익은 시험하지 않았다. 미국장 예보는 다루지 않았다.',
    ]
    ctx = {
        'generatedAt': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'model': model, 'summary': summary, 'wf': wf, 'data': data, 'samples': samples, 'check': check,
        'skipped': skipped, 'skippedDays': skipped_days, 'thresholds': table, 'checks': checks, 'stop': stop,
        'strength': strength_table(wf), 'holiday': holiday_lines(samples, wf, skipped_days, old_wf, open_thr, close_thr),
        'smallYears': small_years, 'thresholdNote': threshold_note,
        'prelimCompare': {'from': prelim_rows[0]['T'], 'to': prelim_rows[-1]['T'], 'days': len(prelim_rows),
                          'stats': window_stats(prelim_rows)},
        'yahooOpen': yahoo_open_effect(wf, data), 'limits': limits,
    }
    args.model_out.write_text(json.dumps(model, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    write_report(args.report, ctx)
    (args.data_dir / 'walk_forward_rows.json').write_text(json.dumps(
        [{k: r[k] for k in ('T', 'P', 'usSessions', 'usBase', 'inputs', 'gap', 'c2c', 'o2c', 'pred')} for r in wf],
        ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'samples': len(samples), 'walkForward': model['walkForward'],
                      'openThreshold': open_thr, 'closeThreshold': close_thr,
                      'openHitPct': overall, 'years': {y: pct(v['openHits'], v['calls']) for y, v in summary['years'].items()},
                      'checks': [(c['name'], c['ok']) for c in checks], 'stop': stop}, ensure_ascii=False, indent=1))
    return 1 if stop or not all(c['ok'] for c in checks) else 0


if __name__ == '__main__':
    sys.exit(main())
