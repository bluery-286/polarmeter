#!/usr/bin/env python3
"""Offline contract: temperature v3 is broad, measured-only, and degrades instead of guessing."""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

import polarmeter_score_core_v3 as v3
from polarmeter_v3_inputs import (
    apply_v3_layers,
    cross_check_kr_previous_close,
    link_signals,
    series_from_chart,
    update_investor_flows,
)

DAYS = [d.isoformat() for d in (date(2025, 9, 1) + timedelta(days=k) for k in range(430)) if d.weekday() < 5]


def synthetic_series(seed: int = 7, overrides: dict[str, list[float]] | None = None) -> dict[str, v3.Series]:
    rng = random.Random(seed)
    out = {}
    for symbol in v3.SYMBOLS + ('QQQ',):
        price, points = 100.0, []
        sd = 0.012 if not symbol.startswith('^VIX') else 0.04
        for day in DAYS:
            price *= 1 + rng.gauss(0.0002, sd)
            points.append((day, round(price, 4)))
        out[symbol] = points
    for symbol, last_moves in (overrides or {}).items():
        points = out[symbol]
        for k, move in enumerate(last_moves, start=len(points) - len(last_moves)):
            points[k] = (points[k][0], points[k - 1][1] * (1 + move / 100))
    return {symbol: v3.Series(points) for symbol, points in out.items()}


def steady_flows(foreign: float, days: list[str]) -> dict[str, dict]:
    return {d: {'kospi': {'foreign': foreign, 'institution': 0.0, 'personal': -foreign},
                'kosdaq': {'foreign': 0.0, 'institution': 0.0, 'personal': 0.0}} for d in days}


def check_series_never_bridges_gaps() -> None:
    s = v3.Series([('2026-11-02', 5859.08), ('2026-11-03', None), ('2026-11-04', 5651.85)])
    assert s.ret(2) is None and s.ret(1) is None  # no two-day move published as one
    assert s.ret(2, 2) is not None


def check_core_shape() -> None:
    core = v3.build_score_core_v3(synthetic_series(), steady_flows(-500.0, DAYS[-70:]), '2026-11-04T01:00:00Z')
    assert core['schemaVersion'] == v3.SCHEMA_VERSION and core['formulaVersion'] == v3.FORMULA_VERSION
    assert core['status'] == 'ok'
    for market, weights in (('us', v3.US_WEIGHTS), ('kr', v3.KR_WEIGHTS)):
        node = core[market]
        assert math.isclose(sum(weights.values()), 1.0) and node['weights'] == weights
        assert set(node['components']) == set(weights) and node['coverage'] == 1.0
        assert all(0 <= value <= 100 for value in node['components'].values())
        assert isinstance(node['score'], int) and 0 <= node['score'] <= 100
        assert not any('placeholder' in key for key in node['components'])
        assert len(core['history'][market]) == v3.HISTORY_SESSIONS
        delta = core['dailyDelta'][market]
        assert delta['delta'] == delta['currentScore'] - delta['previousScore'] == node['score'] - core['history'][market][-1]['score']
        assert set(core['componentLabels'][market]) == set(weights)


def check_missing_inputs_drop_out() -> None:
    series = synthetic_series()
    for symbol in ('XLK', 'XLE', 'XLB', 'XLRE', 'XLC', 'HYG', 'IEF', '^TNX'):
        series.pop(symbol)
    node = v3.market_score('us', series, {}, DAYS[-1])
    assert node['components']['breadth'] is None and node['components']['credit_rates'] is None
    assert math.isclose(node['coverage'], 1 - v3.US_WEIGHTS['breadth'] - v3.US_WEIGHTS['credit_rates'])
    thin = {symbol: series[symbol] for symbol in v3.US_TREND}
    assert v3.market_score('us', thin, {}, DAYS[-1]) is None  # 25% measured is not a temperature


def check_kr_selloff_and_tech_only_day() -> None:
    calm = synthetic_series()
    selloff = synthetic_series(overrides={'^KS11': [-4.0], '^KQ11': [-4.5], **{s: [-4.0] for s in v3.KR_SECTORS + v3.KR_LEADERS}})
    kr_calm = v3.market_score('kr', calm, {}, DAYS[-1])
    kr_sell = v3.market_score('kr', selloff, {}, DAYS[-1])
    assert kr_sell['rawScore'] < kr_calm['rawScore'] - 12 and kr_sell['rawScore'] < 40, (kr_calm, kr_sell)
    assert kr_sell['score'] <= 35 and 'broad_selloff_cap' in kr_sell['guardrail']['applied']
    # A tech-only rally (XLK and chips up, everything broad flat) must not heat the US temperature.
    tech = synthetic_series(overrides={'XLK': [3.0], 'SOXX': [4.0], 'QQQ': [2.5]})
    us_calm = v3.market_score('us', calm, {}, DAYS[-1])
    us_tech = v3.market_score('us', tech, {}, DAYS[-1])
    assert abs(us_tech['rawScore'] - us_calm['rawScore']) < 5, (us_calm, us_tech)


def check_flow_signal() -> None:
    days = DAYS[-40:]
    selling = steady_flows(-9000.0, days)
    assert v3.flow_signal(selling, days[-1]) < -0.5  # steady selling stays pressure, not a new normal
    assert v3.flow_signal(steady_flows(-9000.0, days[-10:]), days[-1]) is None  # too little history
    buying = {**selling, days[-1]: steady_flows(9000.0, [days[-1]])[days[-1]]}
    assert v3.flow_signal(buying, days[-1]) > 0.5


def check_flow_ledger() -> None:
    calls = []

    def fetch(url):
        calls.append(url)
        day = url.rsplit('=', 1)[1]
        return {'bizdate': day, 'foreignValue': '-1,000', 'institutionalValue': '+200', 'personalValue': '+800'}
    sessions = DAYS[-90:]
    now = datetime.fromisoformat(f'{sessions[-1]}T02:00:00+00:00')  # 11:00 KST, session open
    ledger = update_investor_flows(None, sessions, now, fetch, pause=0)
    dates = [item['date'] for item in ledger['items']]
    assert dates[-1] == sessions[-1] and len(dates) == 1 + 25  # today plus newest backfill first
    assert ledger['items'][-1]['settled'] is False and all(item['settled'] for item in ledger['items'][:-1])
    assert ledger['items'][-1]['kospi'] == {'foreign': -1000.0, 'institution': 200.0, 'personal': 800.0}
    calls.clear()
    later = update_investor_flows(ledger, sessions, now + timedelta(hours=6), fetch, pause=0)
    fetched_days = {url.rsplit('=', 1)[1] for url in calls}
    assert sessions[-1].replace('-', '') in fetched_days  # today's intraday row is refetched
    assert not fetched_days & {d.replace('-', '') for d in dates[:-1]}  # settled rows are kept
    assert len(later['items']) <= v3.FLOW_HISTORY_DAYS + 20 and later['items'][-1]['settled'] is True


def check_previous_close_cross_check() -> None:
    # 2026-11-04 01:08Z: Yahoo base was the 11/02 close because the 11/03 bar was null.
    rows = {'KOSPI': [{'localTradedAt': '2026-11-04', 'closePrice': '5,651.85'}, {'localTradedAt': '2026-11-03', 'closePrice': '5,648.79'}],
            'KOSDAQ': [{'localTradedAt': '2026-11-04', 'closePrice': '698.55'}, {'localTradedAt': '2026-11-03', 'closePrice': '704.39'}]}

    def fetch(url):
        return rows['KOSDAQ' if 'KOSDAQ' in url else 'KOSPI']
    signals = {'kospi': {'value': 5651.85, 'previousClose': 5859.08, 'changePct': -3.54, 'dataAsOf': '2026-11-04T01:08:10Z'},
               'kosdaq': {'value': 698.55, 'previousClose': 704.39, 'changePct': -0.83, 'dataAsOf': '2026-11-04T01:08:10Z'}}
    applied = cross_check_kr_previous_close(signals, datetime(2026, 11, 4, 1, 21, tzinfo=timezone.utc), fetch)
    assert [row['key'] for row in applied] == ['kospi']
    assert signals['kospi']['previousClose'] == 5648.79 and abs(signals['kospi']['changePct'] - 0.0542) < 0.001
    assert signals['kosdaq']['changePct'] == -0.83  # matching base is left alone
    yesterday = {'kospi': {'value': 5648.79, 'previousClose': 5859.08, 'changePct': -3.59, 'dataAsOf': '2026-11-03T06:30:00Z'}}
    assert cross_check_kr_previous_close(yesterday, datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc), fetch) == []


def check_link_signals() -> None:
    series = synthetic_series(overrides={'EWY': [1.2, 0.0], 'SOXX': [2.5, 0.0], '091160.KS': [-1.8], 'KRW=X': [0.8]})
    flows = steady_flows(-5000.0, DAYS[-40:])
    rows = {row['key']: row for row in link_signals(series, flows, DAYS[-1])}
    assert list(rows) == ['usd_krw', 'ewy', 'foreign_flow', 'semis_link']
    assert rows['usd_krw']['tone'] == 'minus' and rows['ewy']['tone'] == 'plus'
    assert rows['foreign_flow']['value'] == '-5,000억' and rows['foreign_flow']['tone'] == 'minus'
    assert rows['semis_link']['tone'] == 'neutral' and '이어지지' in rows['semis_link']['readKo']
    assert all(row['readKo'] and row['readEn'] and row['nameEn'] for row in rows.values())


def check_failure_keeps_v2_publishing() -> None:
    def broken(url):
        raise OSError('network down')
    snapshot = {'generatedAt': '2026-11-04T01:00:00Z', 'signals': {}}
    out = apply_v3_layers(snapshot, {'investorFlows': {'items': [{'date': '2026-11-03'}]}}, fetch=broken, pause=0)
    assert out['scoreCoreV3']['status'] == 'unavailable'
    assert out['investorFlows']['status'] == 'unavailable' and out['linkSignals'] == []


def check_review_fixes() -> None:
    # QQQ is fetched, so history applies the same US guardrails as the live score.
    assert 'QQQ' in v3.SYMBOLS
    # A 30% oil move cannot push the component below zero.
    shock = synthetic_series(overrides={'CL=F': [30.0]})
    node = v3.market_score('us', shock, {}, DAYS[-1])
    assert 0 <= node['components']['dollar_commodity'] <= 100 and 0 <= node['score'] <= 100
    # A failed session calendar keeps the flow ledger instead of wiping it.
    previous = {'status': 'ok', 'items': [{'date': d, 'settled': True, 'kospi': {'foreign': -1.0}} for d in DAYS[-30:]]}
    kept = update_investor_flows(previous, [], datetime(2026, 11, 4, 2, tzinfo=timezone.utc), lambda url: {}, pause=0)
    assert kept['status'] == 'unavailable' and len(kept['items']) == 30
    # The latest quote updates its own session even when the daily bar lags; earlier bars stay.
    def chart(closes, quote_ts, price):
        return {'chart': {'result': [{'meta': {'exchangeTimezoneName': 'America/New_York', 'regularMarketPrice': price,
                                               'regularMarketTime': quote_ts},
                                      'timestamp': [1762180200, 1762266600],  # 11/03, 11/04 09:30 ET
                                      'indicators': {'quote': [{'close': closes}]}}]}}
    stale = series_from_chart(chart([100.0, 101.0], 1762290000, 110.0))  # 11/04 16:00 ET quote
    assert stale.dates[-1] == '2025-11-04' and stale.closes[-1] == 110.0 and stale.closes[-2] == 100.0
    missing = series_from_chart(chart([100.0, 101.0], 1762376400, 112.0))  # next day, bar absent
    assert missing.dates[-1] == '2025-11-05' and missing.closes[-1] == 112.0 and missing.closes[-2] == 101.0
    old_quote = series_from_chart(chart([100.0, 101.0], 1762200000, 99.0))  # quote older than last bar
    assert old_quote.closes == [100.0, 101.0]


def main() -> None:
    check_series_never_bridges_gaps()
    check_core_shape()
    check_missing_inputs_drop_out()
    check_kr_selloff_and_tech_only_day()
    check_flow_signal()
    check_flow_ledger()
    check_previous_close_cross_check()
    check_link_signals()
    check_failure_keeps_v2_publishing()
    check_review_fixes()
    print('PASS temperature v3: measured-only, broad, gap-safe, flows ledger, cross-check, link signals')


if __name__ == '__main__':
    main()
