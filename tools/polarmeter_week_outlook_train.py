#!/usr/bin/env python3
"""Train and walk-forward test the week outlook model. Run by a person, never by the cache workflow.

  PYTHONPATH=tools python3 tools/polarmeter_week_outlook_train.py --data-dir <out>/yahoo-cache [--fetch]

Needs numpy. Raw downloads stay in --data-dir (outside the repository); the model goes to
tools/polarmeter_week_outlook_model.json. Features come from polarmeter_week_outlook, the same functions the
live layer uses. Every hit rate in the model is walk-forward: each day is scored by a formula fitted only on
earlier days (first 750 days to start, refit every 60). The published coefficients are refitted on all days.
The futures tiers come from two years of hourly bars and their cuts are fitted on those same two years.
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np

import polarmeter_week_outlook as wo

START, STEP, EVAL = 750, 60, 1000
HOURLY = ('ES=F', '730d', '1h')


def cache_file(data_dir: Path, symbol: str, interval: str) -> Path:
    name = symbol.replace('^', '_').replace('=', '_') + ('' if interval == '1d' else '_' + interval)
    return data_dir / f'{name}.json'


def load_raw(data_dir: Path, symbol: str, range_: str, interval: str, fetch: bool) -> dict:
    path = cache_file(data_dir, symbol, interval)
    if fetch or not path.exists():
        url = wo.YAHOO_URL.format(symbol=urllib.parse.quote(symbol, safe=''), range=range_, interval=interval)
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        path.write_bytes(urllib.request.urlopen(request, timeout=30).read())
    return json.loads(path.read_text())


def fit(X: np.ndarray, y: np.ndarray, lam: float = 10.0, logit: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    mean, scale = X.mean(0), X.std(0) + 1e-12
    Z = (X - mean) / scale
    if logit:
        t = (y > 0).astype(float)
        Z1 = np.c_[np.ones(len(Z)), Z]
        w = np.zeros(Z1.shape[1])
        R = lam * np.eye(Z1.shape[1]); R[0, 0] = 0
        for _ in range(25):
            p = 1 / (1 + np.exp(-Z1 @ w))
            w -= np.linalg.solve((Z1 * (p * (1 - p))[:, None]).T @ Z1 + R, Z1.T @ (p - t) + R @ w)
        return w[1:], mean, scale, float(w[0])
    w = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (y - y.mean()))
    return w, mean, scale, float(y.mean())


def walk(X: np.ndarray, y: np.ndarray, logit: bool = False) -> np.ndarray:
    pred = np.full(len(y), np.nan)
    for s in range(START, len(y), STEP):
        w, mean, scale, b = fit(X[:s], y[:s], logit=logit)
        e = min(s + STEP, len(y))
        pred[s:e] = ((X[s:e] - mean) / scale) @ w + b
    return pred


def tier_record(score: np.ndarray, y: np.ndarray, expanding: bool) -> dict:
    """Hit rate per tier. With `expanding`, each day's tier comes from the scores before it."""
    mag = np.abs(score)
    ok = np.isfinite(score)
    if expanding:
        rank = np.full(len(y), np.nan)
        for i in range(EVAL, len(y)):
            v = mag[START:i]; v = v[np.isfinite(v)]
            rank[i] = (v < mag[i]).mean()
        ok &= np.isfinite(rank)
    else:
        rank = np.argsort(np.argsort(np.where(ok, mag, -1))) / max(ok.sum() - 1, 1)
    hit = (score > 0) == (y > 0)
    pct = lambda m: round(float(hit[m].mean()) * 100, 1) if m.any() else None
    return {'hitPct': {'clear': pct(ok & (rank >= .8)), 'lean': pct(ok & (rank >= .4) & (rank < .8)), 'none': pct(ok & (rank < .4))},
            'allPct': pct(ok), 'alwaysUpPct': round(float(max((y[ok] > 0).mean(), 1 - (y[ok] > 0).mean())) * 100, 1),
            'sampleDays': int(ok.sum())}


def part_json(names, w, mean, scale, b) -> dict:
    return {'features': list(names), 'intercept': round(b, 8), 'coef': [round(float(v), 8) for v in w],
            'mean': [round(float(v), 8) for v in mean], 'sd': [round(float(v), 8) for v in scale]}


def direction_part(names, X, y, logit: bool) -> dict:
    oos = walk(X, y, logit=logit)
    w, mean, scale, b = fit(X, y, logit=logit)
    final = np.abs(((X - mean) / scale) @ w + b)[-1500:]
    return {**part_json(names, w, mean, scale, b), 'leanAbove': round(float(np.quantile(final, .4)), 8),
            'clearAbove': round(float(np.quantile(final, .8)), 8), **tier_record(oos, y, expanding=True)}


def rule_part(score: np.ndarray, y: np.ndarray) -> dict:
    mag = np.abs(score)
    return {'leanAbove': round(float(np.quantile(mag, .4)), 4), 'clearAbove': round(float(np.quantile(mag, .8)), 4),
            **tier_record(score, y, expanding=False)}


def swing_part(names, X: np.ndarray, target: np.ndarray) -> tuple[dict, np.ndarray]:
    """Linear fit of |move| h sessions ahead; returns the part and its walk-forward predictions."""
    oos = walk(X, target)
    w, mean, scale, b = fit(X, target)
    final = ((X - mean) / scale) @ w + b
    floor = float(np.quantile(final, .02))
    final = np.maximum(final, floor)
    cuts = [float(np.quantile(final[-2500:], q)) for q in (.25, .5, .75)]
    stage = 1 + sum((final > c).astype(int) for c in cuts)
    record = [{'meanAbsPct': round(float(target[stage == s].mean()) * 100, 2),
               'over2Pct': round(float((target[stage == s] >= .02).mean()) * 100, 1)} for s in range(1, 5)]
    part = {**part_json(names, w, mean, scale, b), 'floor': round(floor, 8), 'stageCuts': [round(c, 8) for c in cuts], 'stageRecord': record}
    return part, np.maximum(oos, floor)


def coverage(pred: np.ndarray, actual: np.ndarray) -> float:
    ratio = actual / pred
    inside = [ratio[i] <= np.quantile(ratio[max(START, i - wo.BAND_WINDOW):i], wo.BAND_QUANTILE) for i in range(EVAL, len(actual))]
    return round(float(np.mean(inside)) * 100, 1)


def market_swing(book: wo.Book, key: str, days: list[date], extra: dict[date, dict] | None) -> tuple[dict, dict]:
    rows, dates = [], []
    all_dates = book.dates[key]
    pos = {d: i for i, d in enumerate(all_dates)}
    for day in days:
        values = wo.swing_features(book, key, day)
        if values is None or pos[day] + wo.HORIZONS > len(all_dates) or (extra is not None and day not in extra):
            continue
        rows.append(values); dates.append(day)
    closes = np.array([book.bars[key][d][3] for d in all_dates])
    ret = np.diff(np.log(closes), prepend=np.nan)
    Y = np.array([[ret[pos[d] + h] if pos[d] + h < len(ret) else np.nan for h in range(wo.HORIZONS)] for d in dates])
    keep = np.isfinite(Y).all(1)
    rows, dates, Y = [r for r, k in zip(rows, keep) if k], [d for d, k in zip(dates, keep) if k], Y[keep]
    X = np.array([[r[n] for n in wo.SWING_FEATURES] for r in rows])
    horizons, preds = [], []
    for h in range(wo.HORIZONS):
        part, oos = swing_part(wo.SWING_FEATURES, X, np.abs(Y[:, h]))
        horizons.append(part); preds.append(oos)
    out = {'horizons': horizons}
    record = {'rangeInsidePct': [coverage(preds[h], np.abs(Y[:, h])) for h in range(wo.HORIZONS)], 'sampleDays': len(dates) - EVAL}
    if extra is not None:
        names = wo.SWING_FEATURES + wo.OVERNIGHT_FEATURES
        Xo = np.c_[X, np.array([[extra[d][n] for n in wo.OVERNIGHT_FEATURES] for d in dates])]
        part, oos = swing_part(names, Xo, np.abs(Y[:, 0]))
        out['overnight'] = part
        record['overnightRangeInsidePct'] = coverage(oos, np.abs(Y[:, 0]))
        big = np.abs(Y[EVAL:, 0]) >= .03
        record['bigDays'] = int(big.sum())
        record['bigDaysFlaggedPct'] = round(float((1 + sum((oos[EVAL:] > c).astype(int) for c in part['stageCuts']) >= 3)[big].mean()) * 100, 1)
    # the band the whole path stays inside: the largest excursion over the horizons, in units of the summed swing
    excursion = np.zeros(len(dates))
    for k in range(1, wo.HORIZONS + 1):
        spread = np.sqrt(sum(preds[h] ** 2 for h in range(k)))
        excursion = np.maximum(excursion, np.abs(Y[:, :k].sum(1)) / spread)
    out['pathMultiplier'] = round(float(np.quantile(excursion[-wo.BAND_WINDOW - wo.HORIZONS:-wo.HORIZONS], wo.BAND_QUANTILE)), 4)
    inside = [excursion[i] <= np.quantile(excursion[max(START, i - wo.BAND_WINDOW):i - wo.HORIZONS], wo.BAND_QUANTILE) for i in range(EVAL, len(dates))]
    record['pathInsidePct'] = round(float(np.mean(inside)) * 100, 1)
    return out, record


def futures_parts(book: wo.Book, hourly: list[tuple[int, float]]) -> tuple[dict, dict]:
    first = datetime.fromtimestamp(hourly[0][0], timezone.utc) + timedelta(days=2)
    def rows(key: str, since_of, moments) -> dict[str, tuple[list, list]]:
        out = {name: ([], []) for name in moments}
        dates = book.dates[key]
        for prev, day in zip(dates[:-1], dates[1:]):
            since = since_of(prev)
            if since < first:
                continue
            y = wo.log_ratio(book.bars[key][day][3], book.bars[key][prev][3])
            for name, at in moments.items():
                move = wo.futures_move(hourly, since, at(day))
                if move is not None:
                    out[name][0].append(move); out[name][1].append(y)
        return out
    kst = lambda day, h, m, back=0: datetime.combine(day - timedelta(days=back), time(h, m), wo.KST)
    kr = rows('kr', lambda prev: kst(prev, 15, 30), {'2110': lambda d: kst(d, 21, 10, 1), '0010': lambda d: kst(d, 0, 10)})
    us = rows('spx', wo.us_close_at, {'2110': lambda d: kst(d, 21, 10)})
    make = lambda group: {name: rule_part(np.array(a), np.array(b)) for name, (a, b) in group.items()}
    return make(kr), make(us)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--output', type=Path, default=wo.MODEL_PATH)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    book = wo.Book({key: wo.parse_daily(load_raw(args.data_dir, symbol, '15y', '1d', args.fetch), kind)
                    for key, (symbol, kind) in wo.SYMBOLS.items()})
    hourly = wo.parse_hourly(load_raw(args.data_dir, *HOURLY, args.fetch))

    kr_days = book.dates['kr']
    feats = {d: f for d in kr_days if (f := wo.kr_direction_features(book, d)) is not None}
    days = sorted(feats)
    X = np.array([[feats[d][n] for n in wo.KR_DIRECTION_FEATURES] for d in days])
    prev_close = np.array([book.before('kr', d)[3] for d in days])
    bar = np.array([book.bars['kr'][d] for d in days])
    close_ret, gap = np.log(bar[:, 3] / prev_close), np.log(bar[:, 0] / prev_close)
    kr = {'close': direction_part(wo.KR_DIRECTION_FEATURES, X, close_ret, logit=True),
          'open': direction_part(wo.KR_DIRECTION_FEATURES, X, gap, logit=False),
          'closeAfterOpen': rule_part(gap * 100, close_ret)}
    kr['swing'], kr_swing_record = market_swing(book, 'kr', kr_days, {d: wo.overnight_features(f) for d, f in feats.items()})
    us: dict = {}
    us['swing'], us_swing_record = market_swing(book, 'spx', book.dates['spx'], None)
    kr['futures'], us['futures'] = futures_parts(book, hourly)

    pick = lambda part: {k: part[k] for k in ('hitPct', 'allPct', 'alwaysUpPct', 'sampleDays')}
    model = {'modelVersion': wo.MODEL_VERSION, 'trainedThrough': days[-1].isoformat(),
             'method': {'walkForward': {'startDays': START, 'refitEvery': STEP, 'scoredFrom': days[EVAL].isoformat()},
                        'futuresWindow': 'two years of hourly bars; tier cuts fitted on the same window'},
             'kr': kr, 'us': us,
             'record': {'trainedThrough': days[-1].isoformat(), 'scoredFrom': days[EVAL].isoformat(),
                        'krClose': pick(kr['close']), 'krOpen': pick(kr['open']), 'krCloseAfterOpen': pick(kr['closeAfterOpen']),
                        'krFutures': {k: pick(v) for k, v in kr['futures'].items()},
                        'usFutures': {k: pick(v) for k, v in us['futures'].items()},
                        'krSwing': kr_swing_record, 'usSwing': us_swing_record}}
    args.output.write_text(json.dumps(model, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(json.dumps(model['record'], ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
