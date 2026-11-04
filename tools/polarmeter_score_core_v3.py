#!/usr/bin/env python3
"""PolarMeter temperature formula v3: broad, measured inputs only.

Every input is measured; nothing is a fixed neutral 50. Each series is read in
units of its own recent volatility, so a -2% KOSPI day and a -2% S&P day do not
weigh the same. A missing input drops out and the remaining weights are
renormalised; `coverage` reports the weight that was actually measured.

Inputs are daily closes in exchange-local dates (today's bar may be the live,
delayed quote) and KR investor net buying in 억원. v2 (`polarmeter_score_core`)
stays published unchanged for released apps; see out/v3-design-20261104.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_left
from typing import Any, Iterable

from polarmeter_score_core import kr_broad_market_guardrail, label, us_broad_market_guardrail

SCHEMA_VERSION = 'polarmeter-score-core-v3'
FORMULA_VERSION = 'score_core_v3_broad'
MIN_COVERAGE = 0.6
HISTORY_SESSIONS = 30
FLOW_MIN_HISTORY_DAYS = 20
FLOW_HISTORY_DAYS = 60
# Long-run typical daily move (%) of the trend indices. Their 1-day reading uses
# the mean of this and the recent 60-day volatility, so a +2% KOSPI day still
# reads warm in an unusually volatile year (2026 KOSPI daily sd ~3%).
TREND_REFERENCE_SD = {'SPY': 0.9, 'RSP': 0.9, 'IWM': 1.3, 'DIA': 0.9, '^KS11': 1.0, '^KQ11': 1.2}

US_WEIGHTS = {'trend': 0.25, 'breadth': 0.15, 'volatility': 0.20, 'credit_rates': 0.20,
              'risk_appetite': 0.10, 'dollar_commodity': 0.10}
KR_WEIGHTS = {'trend': 0.30, 'breadth': 0.15, 'flows': 0.15, 'currency': 0.15,
              'leaders': 0.15, 'volatility': 0.10}

US_TREND = ('SPY', 'RSP', 'IWM', 'DIA')
US_SECTORS = ('XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'XLB', 'XLRE', 'XLC')
US_CYCLICAL = ('XLY', 'XLI', 'XLF')
US_DEFENSIVE = ('XLP', 'XLU', 'XLV')
KR_SECTORS = ('091160.KS', '091170.KS', '091180.KS', '117700.KS', '117680.KS', '102970.KS',
              '244580.KS', '305720.KS', '266410.KS')
KR_LEADERS = ('005930.KS', '000660.KS', '005380.KS', '373220.KS', '207940.KS', '105560.KS')
OTHER_SYMBOLS = ('^VIX', '^VIX3M', '^TNX', 'HYG', 'IEF', 'DX-Y.NYB', 'CL=F', 'GLD', 'EWY', 'SOXX',
                 '^KS11', '^KQ11', 'KRW=X')
SYMBOLS = tuple(dict.fromkeys(US_TREND + US_SECTORS + KR_SECTORS + KR_LEADERS + OTHER_SYMBOLS))

COMPONENT_LABELS = {
    'us': {'trend': ('주가 흐름', 'Market trend'), 'breadth': ('시장 폭', 'Breadth'),
           'volatility': ('변동성', 'Volatility'), 'credit_rates': ('신용·금리', 'Credit & rates'),
           'risk_appetite': ('위험 선호', 'Risk appetite'), 'dollar_commodity': ('달러·원자재', 'Dollar & commodities')},
    'kr': {'trend': ('지수 흐름', 'Index trend'), 'breadth': ('업종 폭', 'Sector breadth'),
           'flows': ('외국인·기관 수급', 'Foreign & institutional flows'), 'currency': ('환율', 'Currency'),
           'leaders': ('대표 종목', 'Market leaders'), 'volatility': ('변동성', 'Volatility')},
}
COMPONENT_INPUTS = {
    'us': {'trend': 'S&P500 · 동일가중 S&P500 · 러셀2000 · 다우', 'breadth': '11개 업종',
           'volatility': 'VIX · VIX 3개월', 'credit_rates': '하이일드 대 국채 · 10년물 금리',
           'risk_appetite': '경기 업종 대 방어 업종', 'dollar_commodity': '달러지수 · WTI · 금'},
    'kr': {'trend': '코스피 · 코스닥', 'breadth': '업종 ETF 9종', 'flows': '외국인·기관 순매수 · 미국 상장 한국 ETF',
           'currency': '원/달러', 'leaders': '삼성전자 · SK하이닉스 · 현대차 · LG에너지솔루션 · 삼성바이오로직스 · KB금융',
           'volatility': '코스피 20일 변동성'},
}


class Series:
    """Daily closes keyed by exchange-local ISO date. Null closes stay null."""

    def __init__(self, points: Iterable[tuple[str, float | None]]):
        rows = sorted((str(day), None if close is None else float(close)) for day, close in points)
        self.dates = [day for day, _ in rows]
        self.closes = [close for _, close in rows]

    def index_on_or_before(self, day: str) -> int:
        i = bisect_left(self.dates, day)
        if i < len(self.dates) and self.dates[i] == day:
            return i
        return i - 1

    def index_before(self, day: str) -> int:
        return bisect_left(self.dates, day) - 1

    def ret(self, i: int, n: int = 1) -> float | None:
        if i - n < 0 or i >= len(self.closes):
            return None
        a, b = self.closes[i - n], self.closes[i]
        if a in (None, 0) or b is None:
            return None  # never measure across a missing bar
        return (b / a - 1) * 100

    def sd60(self, i: int) -> float | None:
        rets = [r for r in (self.ret(k) for k in range(max(1, i - 60), i)) if r is not None]
        return statistics.pstdev(rets) if len(rets) >= 40 else None

    def z(self, i: int, n: int, reference_sd: float | None = None) -> float | None:
        sd, r = self.sd60(i), self.ret(i, n)
        if not sd or r is None:
            return None
        if reference_sd:
            sd = (sd + reference_sd) / 2
        return r / (sd * math.sqrt(n))

    def signal(self, i: int, reference_sd: float | None = None) -> float | None:
        """Today 60%, week 25%, month 15%, each in units of the series' own volatility."""
        return wavg([(self.z(i, 1, reference_sd), 0.6), (self.z(i, 5, reference_sd), 0.25),
                     (self.z(i, 20, reference_sd), 0.15)])

    def above_ma20(self, i: int) -> bool | None:
        window = self.closes[i - 19:i + 1] if i >= 19 else []
        if len(window) < 20 or any(v is None for v in window):
            return None
        return window[-1] > sum(window) / 20

    def percentile(self, i: int, window: int = 250) -> float | None:
        if i < 0 or self.closes[i] is None:
            return None
        hist = [v for v in self.closes[max(0, i - window):i + 1] if v is not None]
        if len(hist) < 120:
            return None
        return sum(1 for v in hist if v <= self.closes[i]) / len(hist) * 100


def wavg(parts: Iterable[tuple[float | None, float]]) -> float | None:
    usable = [(v, w) for v, w in parts if v is not None and math.isfinite(v)]
    if not usable:
        return None
    return sum(v * w for v, w in usable) / sum(w for _, w in usable)


def score_from_signal(signal: float | None, scale: float = 16.0, invert: bool = False) -> float | None:
    if signal is None:
        return None
    value = -signal if invert else signal
    return max(0.0, min(100.0, 50 + scale * max(-3.2, min(3.2, value))))


def _signal(series: dict[str, Series], symbol: str, day: str, before: bool = False) -> float | None:
    s = series.get(symbol)
    if not s:
        return None
    i = s.index_before(day) if before else s.index_on_or_before(day)
    return s.signal(i, TREND_REFERENCE_SD.get(symbol)) if i > 0 else None


def _breadth(series: dict[str, Series], symbols: tuple[str, ...], day: str) -> float | None:
    up = above = n = 0
    for symbol in symbols:
        s = series.get(symbol)
        i = s.index_on_or_before(day) if s else -1
        if i < 20 or s.dates[i] != day:
            continue
        r, ma = s.ret(i), s.above_ma20(i)
        if r is None or ma is None:
            continue
        n += 1
        up += r > 0
        above += ma
    if n < len(symbols) * 0.7:
        return None
    return 50 * up / n + 50 * above / n


def _basket_gap(series: dict[str, Series], a: tuple[str, ...], b: tuple[str, ...], day: str) -> float | None:
    def basket(symbols: tuple[str, ...], n: int) -> float | None:
        values = []
        for symbol in symbols:
            s = series.get(symbol)
            if s:
                r = s.ret(s.index_on_or_before(day), n)
                if r is not None:
                    values.append(r)
        return sum(values) / len(values) if len(values) >= len(symbols) - 1 else None
    parts = []
    for n, weight in ((1, 0.6), (5, 0.4)):
        x, y = basket(a, n), basket(b, n)
        if x is not None and y is not None:
            parts.append(((x - y) / math.sqrt(n), weight))
    return wavg(parts)


def us_components(series: dict[str, Series], day: str) -> dict[str, float | None]:
    trend = wavg([(score_from_signal(_signal(series, s, day)), 1.0) for s in US_TREND])
    breadth = _breadth(series, US_SECTORS, day)
    vix, vix3m = series.get('^VIX'), series.get('^VIX3M')
    volatility = None
    if vix:
        i = vix.index_on_or_before(day)
        position = vix.percentile(i)
        term = None
        if vix3m:
            k = vix3m.index_on_or_before(day)
            if i >= 0 and k >= 0 and vix.closes[i] and vix3m.closes[k]:
                term = max(0.0, min(100.0, 50 - (vix.closes[i] / vix3m.closes[k] - 0.9) * 250))
        volatility = wavg([(None if position is None else 100 - position, 0.5),
                           (score_from_signal(vix.signal(i) if i > 0 else None, invert=True), 0.25), (term, 0.25)])
    hyg, ief = series.get('HYG'), series.get('IEF')
    credit = None
    if hyg and ief:
        i, k = hyg.index_on_or_before(day), ief.index_on_or_before(day)
        parts = []
        for n, weight in ((1, 0.6), (5, 0.4)):
            a, b = hyg.ret(i, n), ief.ret(k, n)
            if a is not None and b is not None:
                parts.append(((a - b) / math.sqrt(n), weight))
        gap = wavg(parts)
        credit = score_from_signal(None if gap is None else gap / 0.35)
    credit_rates = wavg([(credit, 0.6), (score_from_signal(_signal(series, '^TNX', day), scale=10, invert=True), 0.4)])
    gap = _basket_gap(series, US_CYCLICAL, US_DEFENSIVE, day)
    risk_appetite = score_from_signal(None if gap is None else gap / 0.6)
    oil = series.get('CL=F')
    oil_z = oil.z(oil.index_on_or_before(day), 1) if oil else None
    oil_shock = None if oil_z is None else 50 - 12 * max(0.0, abs(oil_z) - 1.5)
    dollar_commodity = wavg([(score_from_signal(_signal(series, 'DX-Y.NYB', day), scale=10, invert=True), 0.5),
                             (oil_shock, 0.25), (score_from_signal(_signal(series, 'GLD', day), scale=8, invert=True), 0.25)])
    return {'trend': trend, 'breadth': breadth, 'volatility': volatility, 'credit_rates': credit_rates,
            'risk_appetite': risk_appetite, 'dollar_commodity': dollar_commodity}


def flow_signal(flows: dict[str, dict[str, Any]], day: str) -> float | None:
    """Foreign 60% / institution 40% net buying in units of its typical daily size (KOSPI+KOSDAQ, 억원).

    Not demeaned: sixty days of steady foreign selling is pressure, not a new normal.
    """
    def total(row: dict[str, Any] | None, actor: str) -> float | None:
        if not isinstance(row, dict):
            return None
        values = [((row.get(market) or {}).get(actor)) for market in ('kospi', 'kosdaq')]
        return float(sum(values)) if all(isinstance(v, (int, float)) for v in values) else None
    prior_days = sorted(d for d in flows if d < day)[-FLOW_HISTORY_DAYS:]
    parts = []
    for actor, weight in (('foreign', 0.6), ('institution', 0.4)):
        history = [v for v in (total(flows[d], actor) for d in prior_days) if v is not None]
        today = total(flows.get(day), actor)
        if today is None or len(history) < FLOW_MIN_HISTORY_DAYS:
            continue
        typical = math.sqrt(sum(v * v for v in history) / len(history))
        if typical:
            parts.append((today / typical, weight))
    return wavg(parts)


def kr_components(series: dict[str, Series], flows: dict[str, dict[str, Any]], day: str) -> dict[str, float | None]:
    trend = wavg([(score_from_signal(_signal(series, '^KS11', day)), 0.6),
                  (score_from_signal(_signal(series, '^KQ11', day)), 0.4)])
    breadth = _breadth(series, KR_SECTORS, day)
    # EWY: the US session that ended before this KR session opened.
    flows_score = wavg([(score_from_signal(flow_signal(flows, day)), 0.7),
                        (score_from_signal(_signal(series, 'EWY', day, before=True)), 0.3)])
    fx = series.get('KRW=X')
    currency = None
    if fx:
        i = fx.index_on_or_before(day)
        position = fx.percentile(i)
        currency = wavg([(score_from_signal(fx.signal(i) if i > 0 else None, scale=14, invert=True), 0.7),
                         (None if position is None else 100 - position, 0.3)])
    leaders = wavg([(score_from_signal(_signal(series, s, day)), 1.0) for s in KR_LEADERS])
    kospi = series.get('^KS11')
    volatility = None
    if kospi:
        i = kospi.index_on_or_before(day)

        def realised(k: int) -> float | None:
            rets = [kospi.ret(m) for m in range(k - 19, k + 1)]
            return statistics.pstdev(rets) if k >= 20 and all(r is not None for r in rets) else None
        history = [v for v in (realised(k) for k in range(max(20, i - 250), i + 1)) if v is not None]
        current = realised(i)
        if current is not None and len(history) >= 120:
            volatility = 100 - sum(1 for v in history if v <= current) / len(history) * 100
    return {'trend': trend, 'breadth': breadth, 'flows': flows_score, 'currency': currency,
            'leaders': leaders, 'volatility': volatility}


def _market(components: dict[str, float | None], weights: dict[str, float]) -> tuple[float | None, float]:
    coverage = sum(weights[k] for k, v in components.items() if v is not None)
    raw = wavg([(components[k], weights[k]) for k in weights])
    return (raw if coverage >= MIN_COVERAGE else None), round(coverage, 3)


def market_score(market: str, series: dict[str, Series], flows: dict[str, dict[str, Any]], day: str,
                 index_pct: dict[str, float | None] | None = None) -> dict[str, Any] | None:
    """Score one session date. index_pct overrides the guardrail inputs with validated quotes."""
    weights = US_WEIGHTS if market == 'us' else KR_WEIGHTS
    components = us_components(series, day) if market == 'us' else kr_components(series, flows, day)
    raw, coverage = _market(components, weights)
    if raw is None:
        return None

    def pct(symbol: str) -> float | None:
        if index_pct and symbol in index_pct:
            return index_pct[symbol]
        s = series.get(symbol)
        return s.ret(s.index_on_or_before(day)) if s and s.index_on_or_before(day) >= 0 and s.dates[s.index_on_or_before(day)] == day else None
    if market == 'us':
        score, guardrail = us_broad_market_guardrail(raw, pct('SPY'), pct('QQQ'), pct('IWM'))
    else:
        score, guardrail = kr_broad_market_guardrail(raw, pct('^KS11'), pct('^KQ11'))
    final = int(round(score))
    return {
        'sessionDate': day,
        'score': final,
        'rawScore': round(raw, 1),
        'label': label(final),
        'coverage': coverage,
        'components': {k: (None if v is None else round(v, 1)) for k, v in components.items()},
        'weights': dict(weights),
        'guardrail': {'applied': guardrail.get('applied', [])},
    }


def session_dates(series: dict[str, Series], anchor: str) -> list[str]:
    s = series.get(anchor)
    return [d for d, c in zip(s.dates, s.closes) if c is not None] if s else []


def build_score_core_v3(series: dict[str, Series], flows: dict[str, dict[str, Any]], generated_at: str | None,
                        index_pct: dict[str, float | None] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {'schemaVersion': SCHEMA_VERSION, 'formulaVersion': FORMULA_VERSION,
                           'generatedAt': generated_at, 'status': 'ok', 'history': {}, 'dailyDelta': {},
                           'componentLabels': {m: {k: {'ko': ko, 'en': en} for k, (ko, en) in labels.items()}
                                               for m, labels in COMPONENT_LABELS.items()},
                           'componentInputs': COMPONENT_INPUTS}
    for market, anchor, guard_keys in (('us', 'SPY', ('SPY', 'QQQ', 'IWM')), ('kr', '^KS11', ('^KS11', '^KQ11'))):
        days = session_dates(series, anchor)
        if not days:
            out[market] = None
            continue
        today = days[-1]
        current = market_score(market, series, flows, today,
                               {k: v for k, v in (index_pct or {}).items() if k in guard_keys} or None)
        history = []
        for day in days[-HISTORY_SESSIONS - 1:-1]:
            row = market_score(market, series, flows, day)
            if row:
                history.append({'date': day, 'score': row['score']})
        out[market] = current
        out['history'][market] = history
        previous = history[-1] if history else None
        out['dailyDelta'][market] = None if not (current and previous) else {
            'currentScore': current['score'], 'previousScore': previous['score'],
            'delta': current['score'] - previous['score'], 'comparisonDate': previous['date'],
            'basis': 'previous_session',
        }
    if not out.get('us') or not out.get('kr'):
        out['status'] = 'partial' if out.get('us') or out.get('kr') else 'unavailable'
    return out
