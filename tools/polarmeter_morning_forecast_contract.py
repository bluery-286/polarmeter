#!/usr/bin/env python3
"""Offline contract: morning forecast inputs and prediction (SPEC §6.3, §6.6).

Part 1 (bundle 1-A): the inputs and the prediction. Part 2 (bundle 1-B): the
freeze, window, scoring, scorecard, ledger, pause, copy and failure cases
(SPEC §6.6 items 1-11), all on fixed in-code data with no network.

  PYTHONPATH=tools python3 tools/polarmeter_morning_forecast_contract.py
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import polarmeter_market_calendar as cal
import polarmeter_morning_forecast as mf
import polarmeter_score_core_v3 as v3

# Preliminary coefficients from SPEC §6.2; fixed here so the case does not move when the model is retrained.
FIXED_MODEL = {
    'features': ['ewyExcessPct', 'spyPct', 'soxxPct'],
    'open': {'intercept': 0.067, 'coef': [0.180, 0.199, 0.077], 'callThreshold': 0.3, 'strongThreshold': 1.0},
    'close': {'intercept': -0.009, 'coef': [0.139, 0.237, 0.063], 'callThreshold': 0.6},
    'analogBuckets': [
        {'id': 'open_le_-1.0', 'lo': None, 'hi': -1.0}, {'id': 'open_-1.0_-0.6', 'lo': -1.0, 'hi': -0.6},
        {'id': 'open_-0.6_-0.3', 'lo': -0.6, 'hi': -0.3}, {'id': 'open_0.3_0.6', 'lo': 0.3, 'hi': 0.6},
        {'id': 'open_0.6_1.0', 'lo': 0.6, 'hi': 1.0}, {'id': 'open_ge_1.0', 'lo': 1.0, 'hi': None},
    ],
}


def us(points: dict[str, dict[str, float]]) -> dict[str, v3.Series]:
    """{symbol: {date: close}} -> Series per symbol."""
    return {symbol: v3.Series(rows.items()) for symbol, rows in points.items()}


def same_closes(dates: list[str], closes: list[float]) -> dict[str, dict[str, float]]:
    return {s: dict(zip(dates, closes)) for s in ('SPY', 'SOXX', 'EWY')}


def near(a: float | None, b: float, tol: float = 1e-9) -> bool:
    return a is not None and abs(a - b) <= tol


def check_20261106() -> None:
    # Real closes: SPY/SOXX/EWY -4.41/-12.15/-13.46% on 2026-11-05 (base 100), KOSPI 5,683.80 -> 5,383.80 (-5.28%).
    series = us({'SPY': {'2026-11-04': 100.0, '2026-11-05': 95.59},
                 'SOXX': {'2026-11-04': 100.0, '2026-11-05': 87.85},
                 'EWY': {'2026-11-04': 100.0, '2026-11-05': 86.54}})
    kospi = v3.Series([('2026-11-03', 5648.79), ('2026-11-04', 5683.80), ('2026-11-05', 5383.80)])
    out = mf.forecast_inputs('2026-11-06', '2026-11-05', series, kospi)
    assert out['reason'] is None and out['usSessions'] == ['2026-11-05'] and out['usBaseSession'] == '2026-11-04', out
    assert out['inputs'] == {'spyPct': -4.41, 'soxxPct': -12.15, 'ewyPct': -13.46, 'kospiPrevPct': -5.28,
                             'ewyExcessPct': -8.18}, out['inputs']
    pred = mf.predict(out['inputs'], FIXED_MODEL)
    # 0.067 + 0.180*-8.18 + 0.199*-4.41 + 0.077*-12.15 = -3.2186
    assert pred['openPct'] == -3.22 and pred['strength'] == 'strong' and pred['openCall'] == 'down', pred
    # -0.009 + 0.139*-8.18 + 0.237*-4.41 + 0.063*-12.15 = -2.9604
    assert pred['closePct'] == -2.96 and pred['closeCall'] == 'down', pred
    assert pred['topDriver'] == 'ewyExcessPct' and pred['analogBucket'] == 'open_le_-1.0', pred
    # The trained model file loads, has the SPEC §6.2 shape and calls the same day down.
    model = mf.load_model()
    for key in ('modelVersion', 'trainedThrough', 'features', 'open', 'close', 'openBands', 'analogBuckets',
                'baseRates', 'walkForward', 'dataSources'):
        assert key in model, key
    assert model['modelVersion'] == mf.MODEL_VERSION and tuple(model['features']) == mf.FEATURES
    assert len(model['open']['coef']) == len(model['close']['coef']) == 3
    live = mf.predict(out['inputs'], model)
    assert live['openCall'] == 'down' and live['strength'] == 'strong', live


def check_weekday() -> None:
    # T=Thu 2026-10-29, P=Wed 10-28; one US session (Wed). EWY minus KOSPI's Wednesday move.
    series = us(same_closes(['2026-10-27', '2026-10-28', '2026-10-29'], [100.0, 101.0, 90.0]))
    kospi = v3.Series([('2026-10-27', 1000.0), ('2026-10-28', 1005.0)])
    out = mf.forecast_inputs('2026-10-29', '2026-10-28', series, kospi)
    # The US 10-29 bar (trading after KR 10-29 closed) must not be used.
    assert out['usSessions'] == ['2026-10-28'] and out['reason'] is None, out
    assert out['inputs']['spyPct'] == 1.0 and out['inputs']['kospiPrevPct'] == 0.5
    assert out['inputs']['ewyExcessPct'] == 0.5, out['inputs']


def check_friday_to_monday() -> None:
    # T=Mon 2026-11-02, P=Fri 10-30: the Friday US session only.
    series = us(same_closes(['2026-10-29', '2026-10-30', '2026-11-02'], [200.0, 198.0, 250.0]))
    kospi = v3.Series([('2026-10-29', 1000.0), ('2026-10-30', 990.0)])
    out = mf.forecast_inputs('2026-11-02', '2026-10-30', series, kospi)
    assert out['usSessions'] == ['2026-10-30'] and out['reason'] is None, out
    assert out['inputs']['spyPct'] == -1.0 and out['inputs']['kospiPrevPct'] == -1.0
    assert out['inputs']['ewyExcessPct'] == 0.0, out['inputs']


def check_kr_only_holiday() -> None:
    # KR shut on Fri 2026-10-09 (한글날), US open. T=Mon 10-12, P=Thu 10-08: US Thu and Fri accumulate.
    series = us(same_closes(['2026-10-07', '2026-10-08', '2026-10-09'], [100.0, 102.0, 104.04]))
    kospi = v3.Series([('2026-10-07', 1000.0), ('2026-10-08', 1010.0)])
    out = mf.forecast_inputs('2026-10-12', '2026-10-08', series, kospi)
    assert out['usSessions'] == ['2026-10-08', '2026-10-09'] and out['usBaseSession'] == '2026-10-07', out
    assert out['inputs']['spyPct'] == 4.04, out['inputs']  # 1.02 * 1.02 - 1, not 2% + 2%
    assert out['inputs']['kospiPrevPct'] == 1.0 and out['inputs']['ewyExcessPct'] == 3.04, out['inputs']


def check_us_holiday() -> None:
    # US shut on Mon 2026-01-19 (MLK Day), KR open. T=Tue 01-20, P=Mon 01-19: no US session, no forecast.
    series = us(same_closes(['2026-01-15', '2026-01-16', '2026-01-20'], [100.0, 99.0, 98.0]))
    kospi = v3.Series([('2026-01-16', 1000.0), ('2026-01-19', 1010.0)])
    out = mf.forecast_inputs('2026-01-20', '2026-01-19', series, kospi)
    assert out['reason'] == 'us_market_closed' and out['inputs'] is None and out['usSessions'] == [], out


def check_after_us_only_holiday() -> None:
    # US shut on Mon 2026-01-19 (MLK Day), KR open both days. T=Wed 01-21, P=Tue 01-20.
    # EWY's Tue change is measured from Fri 01-16, so it already carries KOSPI's Mon and Tue moves:
    # both are subtracted (SPEC §6.3, revised 2026-11-06), not Tue alone.
    series = us(same_closes(['2026-01-15', '2026-01-16', '2026-01-20', '2026-01-21'], [100.0, 100.0, 104.0, 90.0]))
    kospi = v3.Series([('2026-01-15', 990.0), ('2026-01-16', 1000.0), ('2026-01-19', 1010.0), ('2026-01-20', 1030.2)])
    out = mf.forecast_inputs('2026-01-21', '2026-01-20', series, kospi)
    assert out['reason'] is None and out['usSessions'] == ['2026-01-20'] and out['usBaseSession'] == '2026-01-16', out
    assert out['krSessionsInEwy'] == ['2026-01-19', '2026-01-20'], out
    assert out['inputs']['ewyPct'] == 4.0 and out['inputs']['kospiPrevPct'] == 3.02, out['inputs']  # 1.01 * 1.02 - 1
    assert out['inputs']['ewyExcessPct'] == 0.98, out['inputs']  # the first design gave 4.0 - 2.0 = 2.0
    # No KR session after the EWY base: nothing to subtract.
    assert mf.kospi_since(kospi, '2026-01-20', '2026-01-20') == (0.0, [])
    # P missing from the KOSPI closes is a data gap, not zero.
    assert mf.kospi_since(v3.Series([('2026-01-16', 1000.0)]), '2026-01-15', '2026-01-20')[0] is None


def check_broken_inputs() -> None:
    dates = ['2026-10-27', '2026-10-28']
    kospi = v3.Series([('2026-10-27', 1000.0), ('2026-10-28', 1005.0)])
    points = same_closes(dates, [100.0, 101.0])
    points['SOXX'] = {'2026-10-27': 100.0}  # SOXX has no 10-28 bar
    out = mf.forecast_inputs('2026-10-29', '2026-10-28', us(points), kospi)
    assert out['reason'] == 'us_session_mismatch' and out['inputs'] is None, out
    points = same_closes(dates, [100.0, 101.0])
    points['EWY']['2026-10-28'] = None
    out = mf.forecast_inputs('2026-10-29', '2026-10-28', us(points), kospi)
    assert out['reason'] == 'input_missing', out
    out = mf.forecast_inputs('2026-10-29', '2026-10-28', {'SPY': us(points)['SPY']}, kospi)
    assert out['reason'] == 'us_series_missing', out
    out = mf.forecast_inputs('2026-10-29', '2026-10-28', us(same_closes(dates, [100.0, 101.0])),
                             v3.Series([('2026-10-28', 1005.0)]))
    assert out['reason'] == 'kospi_prev_missing', out


def check_calls_and_buckets() -> None:
    def run(e: float, s: float, x: float) -> dict:
        return mf.predict({'ewyExcessPct': e, 'spyPct': s, 'soxxPct': x}, FIXED_MODEL)
    # The call is made on the rounded number: 0.067 + 0.18*1.294 = 0.29992 -> 0.30 -> moderate up.
    p = run(1.294, 0.0, 0.0)
    assert p['openPct'] == 0.3 and p['strength'] == 'moderate' and p['openCall'] == 'up', p
    assert p['analogBucket'] == 'open_0.3_0.6', p
    p = run(0.0, 0.0, 0.0)
    assert p['strength'] == 'weak' and p['openCall'] == 'none' and p['analogBucket'] is None, p
    assert p['closeCall'] == 'none', p
    p = run(-5.927, 0.0, 0.0)  # 0.067 - 1.06686 = -0.99986 -> -1.0 -> strong, bottom bucket
    assert p['openPct'] == -1.0 and p['strength'] == 'strong' and p['analogBucket'] == 'open_le_-1.0', p
    p = run(-2.04, 0.0, 0.0)  # 0.067 - 0.3672 = -0.3002 -> -0.30 -> moderate down, (-0.6, -0.3]
    assert p['openPct'] == -0.3 and p['openCall'] == 'down' and p['analogBucket'] == 'open_-0.6_-0.3', p




# ============================================================================
# Part 2 (bundle 1-B): freeze, window, scoring, scorecard, ledger, pause, copy, failure
# ============================================================================

KST = ZoneInfo('Asia/Seoul')

# SPEC §6.2 preliminary numbers, completed with the extra tables the layer reads. Fixed here so the copy numbers
# (46 / 53 / 84 / 71 / 47) do not move when the model is retrained.
FULL_MODEL = {
    **FIXED_MODEL, 'modelVersion': 'kospi-overnight-v1',
    'openBands': [{'maxAbs': 0.3, 'p50AbsErr': 0.4, 'days': 1}, {'maxAbs': 1.0, 'p50AbsErr': 0.6, 'days': 1},
                  {'maxAbs': None, 'p50AbsErr': 1.15, 'days': 1}],
    'analogBuckets': [
        {'id': 'open_le_-1.0', 'lo': None, 'hi': -1.0, 'days': 36, 'openDown': 33, 'closeDown': 28},
        {'id': 'open_-1.0_-0.6', 'lo': -1.0, 'hi': -0.6, 'days': 30, 'openDown': 25, 'closeDown': 20},
        {'id': 'open_-0.6_-0.3', 'lo': -0.6, 'hi': -0.3, 'days': 12, 'openDown': 8, 'closeDown': 6},
        {'id': 'open_0.3_0.6', 'lo': 0.3, 'hi': 0.6, 'days': 10, 'openUp': 7, 'closeUp': 5},
        {'id': 'open_0.6_1.0', 'lo': 0.6, 'hi': 1.0, 'days': 25, 'openUp': 22, 'closeUp': 17},
        {'id': 'open_ge_1.0', 'lo': 1.0, 'hi': None, 'days': 40, 'openUp': 37, 'closeUp': 30},
    ],
    'baseRates': {'gapDownIntradayRebound': {'gapAtMost': -1.5, 'days': 74, 'rebounded': 34},
                  'gapUpIntradayExtend': {'gapAtLeast': 1.5, 'days': 85, 'extended': 45},
                  'moderateOpenHitPct': 84, 'moderateCloseHitPct': 71, 'weakCloseHitPct': 47, 'intradayHitPct': 50},
    'walkForward': {'from': '2024-11-06', 'to': '2026-11-06', 'days': 472, 'calls': 281, 'openHits': 237, 'noCallDays': 191},
}

# ---------------------------------------------------------------- a fixed world (daily bars for 2026, no network)

US_RET = {'2026-11-05': (-4.41, -12.15, -13.46)}                    # SPY, SOXX, EWY daily % (2026-11-06 case)
US_RET_DEFAULT = (0.05, 0.05, 0.05)
KR_MOVES = {'2026-11-05': (-1.0, -5.28), '2026-11-06': (-2.24, -6.02)}  # (open gap %, close %) vs the previous close
KR_MOVES_DEFAULT = (0.1, 0.05)


def weekdays(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def build_world() -> dict:
    us_days = [d.isoformat() for d in weekdays(date(2026, 1, 1), date(2026, 12, 4))
               if cal.us_market_closed_reason(datetime(d.year, d.month, d.day, 12, tzinfo=cal.NY_ZONE)) is None]
    us = {s: {} for s in ('SPY', 'SOXX', 'EWY')}
    levels = {'SPY': 400.0, 'SOXX': 200.0, 'EWY': 60.0}
    for day in us_days:
        for i, symbol in enumerate(('SPY', 'SOXX', 'EWY')):
            levels[symbol] *= 1 + US_RET.get(day, US_RET_DEFAULT)[i] / 100
            us[symbol][day] = round(levels[symbol], 4)
    kr, level = {}, 3000.0
    for d in weekdays(date(2026, 1, 1), date(2026, 12, 4)):
        if not mf.kr_open_day(d):
            continue
        gap, ret = KR_MOVES.get(d.isoformat(), KR_MOVES_DEFAULT)
        kr[d.isoformat()] = {'open': round(level * (1 + gap / 100), 2), 'close': round(level * (1 + ret / 100), 2),
                             'source': 'naver_index_price'}
        level = kr[d.isoformat()]['close']
    return {'us': us, 'kr': kr}


WORLD = build_world()


def view(now: datetime, drop: tuple[str, ...] = (), us_edit: dict | None = None) -> tuple[dict, dict]:
    """What the feeds would show at `now`: Yahoo bars up to the New York date of `now` (today's bar included, as Yahoo
    does while the session trades) and Naver rows up to today in Seoul (today's row included, close and all)."""
    ny_today = now.astimezone(cal.NY_ZONE).date().isoformat()
    kst_today = now.astimezone(KST).date().isoformat()
    series = {}
    for symbol, closes in WORLD['us'].items():
        if symbol in drop:
            continue
        rows = {d: c for d, c in closes.items() if d <= ny_today}
        rows.update((us_edit or {}).get(symbol, {}))
        series[symbol] = v3.Series(rows.items())
    prices = {d: dict(r) for d, r in WORLD['kr'].items() if d <= kst_today}
    return series, prices


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=KST)


def run(now: str, previous: dict | None = None, model: dict = FULL_MODEL, drop: tuple[str, ...] = (),
        us_edit: dict | None = None, prices_edit=None) -> dict:
    moment = at(now)
    series, prices = view(moment, drop, us_edit)
    if prices_edit:
        prices_edit(prices)
    block = mf.compute_block({'morningForecast': previous} if previous else None, series, prices, moment, model)
    mf.assert_public_morning_forecast(block)
    return json.loads(json.dumps(block, ensure_ascii=False, allow_nan=False))  # as published, then read back


def row_of(block: dict, target: str) -> dict:
    return next(r for r in block['ledger'] if r['targetSession'] == target)


def sample_block() -> dict:
    """The 2026-11-06 morning block (fixed model), for the security and pages contracts."""
    return run('2026-11-06T06:45:00')


# ---------------------------------------------------------------- SPEC §6.6 cases

def check_case1_hand_calculation() -> None:
    block = run('2026-11-06T06:45:00')
    cur = block['current']
    assert cur['targetSession'] == '2026-11-06' and cur['basisKrSession'] == '2026-11-05' and cur['state'] == 'issued', cur
    assert cur['inputs'] == {'spyPct': -4.41, 'soxxPct': -12.15, 'ewyPct': -13.46, 'kospiPrevPct': -5.28,
                             'ewyExcessPct': -8.18}, cur['inputs']
    p = cur['predicted']
    assert p['openPct'] == -3.22 and p['closePct'] == -2.96 and p['strength'] == 'strong', p
    assert p['openCall'] == 'down' and p['closeCall'] == 'down' and p['topDriver'] == 'ewyExcessPct', p
    assert p['openBand'] == [-4.37, -2.07], p
    assert cur['analog'] == {'bucket': 'open_le_-1.0', 'days': 36, 'openSame': 33, 'closeSame': 28,
                             'period': '2024-11 ~ 2026-11', 'widened': False}, cur['analog']
    assert cur['issuedAt'] == '2026-11-05T21:45:00Z' and cur['origin'] == 'live' and cur['usSessions'] == ['2026-11-05']
    assert block['status'] == 'ok' and block['phase'] == 'pre_open', (block['status'], block['phase'])
    assert block['nextIssueAt'] == '2026-11-06T21:45:00Z', block['nextIssueAt']  # Fri's US close 06:00 + 45 min, Sat KST
    # The exact issue minute: 15 minutes after the close is not enough, the feed grace (45) decides.
    assert run('2026-11-06T06:44:00')['current'] is None or run('2026-11-06T06:44:00')['current']['targetSession'] != '2026-11-06'
    assert cal.last_completed_session_date(at('2026-11-06T06:44:00'), 'US').isoformat() == '2026-11-04'
    assert mf.us_session_ready_at('2026-11-05') == at('2026-11-06T06:45:00')


def check_case2_freeze() -> None:
    first = run('2026-11-06T06:45:00')
    frozen = first['current']
    # A later run sees different (even absurd) overnight numbers; nothing issued may move.
    edited = {'SPY': {'2026-11-05': 999.0}, 'SOXX': {'2026-11-05': 1.0}, 'EWY': {'2026-11-05': 1.0}}
    second = run('2026-11-06T08:30:00', first, us_edit=edited)
    cur = second['current']
    for key in ('inputs', 'predicted', 'issuedAt', 'analog', 'usSessions', 'origin'):
        assert cur[key] == frozen[key], (key, cur[key], frozen[key])
    assert row_of(second, '2026-11-06')['predicted'] == frozen['predicted']
    # Even with the series gone and the ledger-only rows read back (no `inputs` in the ledger), the frozen row stays.
    third = run('2026-11-06T08:40:00', second, drop=('SPY', 'SOXX', 'EWY'))
    assert third['current']['inputs'] == frozen['inputs'] and third['current']['issuedAt'] == frozen['issuedAt']


def check_case3_no_issue_while_us_trades() -> None:
    # Thu 2026-11-05 05:00 KST = Wed 15:00 EST: Wed's session is open and Yahoo already shows Wed's bar.
    block = run('2026-11-05T05:00:00')
    assert not any(r['targetSession'] == '2026-11-05' for r in block['ledger']), 'nothing may be issued while the US trades'
    assert all(r['origin'] == 'backfill' for r in block['ledger']) and block['current'] is None
    assert block['phase'] == 'off' and block['status'] == 'ok'
    # 06:30 KST: the bell has rung but the feed grace is not over.
    assert not any(r['targetSession'] == '2026-11-05' for r in run('2026-11-05T06:30:00')['ledger'])
    assert row_of(run('2026-11-05T06:46:00'), '2026-11-05')['state'] == 'issued'
    # phase while the US trades, with a forecast already on the card (Fri 2026-11-06 23:30 KST = 09:30 EST, session open)
    after_close = run('2026-11-06T16:00:00', run('2026-11-06T06:45:00'))
    assert run('2026-11-06T23:30:00', after_close)['phase'] == 'us_open'


def check_case4_missed_window() -> None:
    # SOXX never arrived: wait while it can still come, then record missed_window at 08:50.
    ledger = run('2026-11-04T16:00:00')  # yesterday's run, so this is a normal run and not a first deployment
    waiting = run('2026-11-05T08:49:00', ledger, drop=('SOXX',))
    assert not any(r['targetSession'] == '2026-11-05' for r in waiting['ledger'])
    missed = run('2026-11-05T08:50:00', waiting, drop=('SOXX',))
    row = row_of(missed, '2026-11-05')
    assert row['state'] == 'no_forecast' and row['noForecastReason'] == 'missed_window' and row['predicted'] is None, row
    assert missed['current']['targetSession'] == '2026-11-05' and missed['current']['copy']['title']['ko'] == \
        '자료가 덜 들어와 오늘 예보를 내지 못했어요', missed['current']['copy']
    # Complete data at 08:50 is still too late.
    late = run('2026-11-05T08:50:00')
    assert row_of(late, '2026-11-05')['noForecastReason'] == 'missed_window', row_of(late, '2026-11-05')
    # 08:49 with complete data still issues.
    assert row_of(run('2026-11-05T08:49:00'), '2026-11-05')['state'] == 'issued'
    # A run that only happens after the open still leaves a line for the day it missed (the gap rule).
    prev = run('2026-11-04T07:00:00')
    gap = run('2026-11-05T09:30:00', prev)
    assert row_of(gap, '2026-11-05')['noForecastReason'] == 'missed_window' and row_of(gap, '2026-11-05')['origin'] == 'live'
    # A suspect input (over 15%) is recorded, not forecast.
    crash = run('2026-11-05T07:00:00', us_edit={'SOXX': {'2026-11-04': 1.0}})
    assert row_of(crash, '2026-11-05')['noForecastReason'] == 'inputs_suspect', row_of(crash, '2026-11-05')


def check_case5_holidays() -> None:
    # US shut Thu 2026-11-26 (Thanksgiving), KR open. T=Fri 11-27, P=Thu 11-26: no US session.
    early = run('2026-11-27T06:00:00')
    assert not any(r['targetSession'] == '2026-11-27' for r in early['ledger']), 'the answer is not due before the slot'
    block = run('2026-11-27T06:45:00')
    row = row_of(block, '2026-11-27')
    assert row['state'] == 'no_forecast' and row['noForecastReason'] == 'us_market_closed' and row['usSessions'] == [], row
    assert block['current']['copy']['title']['ko'] == '밤사이 미국장이 쉬어서 예보가 없어요'
    assert block['current']['copy']['title']['en'] == 'No forecast: US markets were closed'
    # KR shut Thu-Fri 2026-09-24/25 (Chuseok): after Wed 09-23 the target jumps to Mon 09-28 and the US sessions pile up.
    assert mf.target_session_for(at('2026-09-23T20:00:00')) == date(2026, 9, 28)
    assert mf.target_session_for(at('2026-09-28T08:59:00')) == date(2026, 9, 28)
    assert mf.target_session_for(at('2026-09-28T09:00:00')) == date(2026, 9, 29)
    assert mf.us_calendar_sessions('2026-09-23', '2026-09-28') == ['2026-09-23', '2026-09-24', '2026-09-25']
    block = run('2026-09-26T06:00:00')  # Sat: Fri 09-25 closed at 05:00 KST (EDT), +45 min
    cur = block['current']
    assert cur['targetSession'] == '2026-09-28' and cur['basisKrSession'] == '2026-09-23', cur
    assert cur['usSessions'] == ['2026-09-23', '2026-09-24', '2026-09-25'] and cur['state'] == 'issued', cur
    assert abs(cur['inputs']['spyPct'] - round((1.0005 ** 3 - 1) * 100, 2)) < 1e-9, cur['inputs']  # compounded, not summed


def check_case6_friday_to_monday() -> None:
    # Fri 2026-10-30 US session, issued Sat 10-31 for Mon 11-02 (EDT still: the clocks change on 11-01).
    assert run('2026-10-31T05:30:00')['current'] is None
    block = run('2026-10-31T08:00:00')
    cur = block['current']
    assert cur['targetSession'] == '2026-11-02' and cur['basisKrSession'] == '2026-10-30' and cur['usSessions'] == ['2026-10-30'], cur
    assert block['phase'] == 'pre_open' and cur['copy']['title']['ko'].startswith('월요일'), cur['copy']['title']
    assert cur['state'] == 'issued' and cur['issuedAt'] == '2026-10-30T23:00:00Z'
    # The Sunday run keeps it; Monday 08:50 does not re-issue it.
    again = run('2026-11-02T08:40:00', block)
    assert again['current']['issuedAt'] == cur['issuedAt']
    # After the US-only holiday rule: MLK 2026-01-19, KR open both days, T=Wed 01-21 -> afterUsHoliday.
    block = run('2026-01-21T07:00:00')
    assert row_of(block, '2026-01-21').get('afterUsHoliday') is True, row_of(block, '2026-01-21')
    assert 'afterUsHoliday' not in row_of(run('2026-11-06T06:45:00'), '2026-11-06')


def check_case7_scoring() -> None:
    issued = run('2026-11-06T06:45:00')
    before = run('2026-11-06T09:04:00', issued)
    assert row_of(before, '2026-11-06')['state'] == 'issued' and row_of(before, '2026-11-06')['actual'] is None
    opened = run('2026-11-06T09:05:00', before)
    row = row_of(opened, '2026-11-06')
    assert row['state'] == 'open_scored' and row['actual']['openPct'] == -2.24 and row['actual']['closePct'] is None, row
    assert row['actual']['source'] == 'naver_index_price' and row['result'] == {'openHit': True, 'closeHit': None, 'openInBand': True}, row
    assert opened['phase'] == 'kr_open' and opened['current']['copy']['openResult']['ko'] == '시가 −2.2%, 예보 적중'
    wait = run('2026-11-06T15:39:00', opened)  # the Naver row already holds the final close; the gate must hold
    assert row_of(wait, '2026-11-06')['state'] == 'open_scored' and row_of(wait, '2026-11-06')['actual']['closePct'] is None
    closed = run('2026-11-06T15:40:00', wait)
    row = row_of(closed, '2026-11-06')
    assert row['state'] == 'closed_scored' and row['actual']['closePct'] == -6.02 and row['actual']['intradayPct'] == -3.87, row
    assert row['result'] == {'openHit': True, 'closeHit': True, 'openInBand': True}, row
    assert closed['phase'] == 'kr_closed' and 'resultRows' in closed['current']['copy']
    # No call -> no hit either way. 2026-11-04 is a default (weak) day.
    weak = run('2026-11-04T07:00:00')
    day = run('2026-11-04T16:00:00', weak)
    row = row_of(day, '2026-11-04')
    assert row['predicted']['openCall'] == 'none' and row['result']['openHit'] is None, row
    # A flat open scores a miss for a call; a missing open price is retried, then void after 3 KR trading days.
    def flat(prices: dict) -> None:
        prices['2026-11-06']['open'] = prices['2026-11-05']['close']
    zero = run('2026-11-06T09:05:00', issued, prices_edit=flat)
    assert row_of(zero, '2026-11-06')['result']['openHit'] is False and row_of(zero, '2026-11-06')['actual']['openPct'] == 0.0

    def no_open(prices: dict) -> None:
        for day in ('2026-11-06', '2026-11-09', '2026-11-10', '2026-11-11'):
            if day in prices:
                prices[day]['open'] = None
                prices[day]['close'] = None
    held = run('2026-11-10T09:30:00', issued, prices_edit=no_open)  # 11-09 and 11-10 so far: 2 trading days
    assert row_of(held, '2026-11-06')['state'] == 'issued'
    voided = run('2026-11-11T09:30:00', held, prices_edit=no_open)
    assert row_of(voided, '2026-11-06')['state'] == 'void', row_of(voided, '2026-11-06')
    assert row_of(voided, '2026-11-06')['predicted'] == issued['current']['predicted'], 'void keeps what was frozen'
    # Yahoo fallback is reported in `source` (rows produced by the Yahoo reader carry it).
    def yahoo(prices: dict) -> None:
        prices['2026-11-06']['source'] = 'yahoo_chart_daily'
    fell = run('2026-11-06T09:05:00', issued, prices_edit=yahoo)
    assert row_of(fell, '2026-11-06')['actual']['source'] == 'yahoo_chart_daily'


def make_rows(count: int, hits: int, calls: int, live_from: int = 10**9, before: str = '2026-09-01') -> dict:
    """A previous public block with `count` scored rows ending right before `before`: the first `calls` rows are
    calls, the first `hits` of those hit."""
    days = list(reversed(mf.trading_days_back(date.fromisoformat(before), count)))
    rows = []
    for i, d in enumerate(days):
        called = i < calls
        rows.append({'targetSession': d.isoformat(), 'basisKrSession': mf.kr_day_before(d).isoformat(), 'usSessions': [],
                     'origin': 'live' if i >= live_from else 'backfill', 'state': 'closed_scored', 'issuedAt': None,
                     'noForecastReason': None,
                     'predicted': {'openPct': -1.0 if called else 0.0, 'closePct': -1.0 if called else 0.0, 'openBand': None,
                                   'strength': 'strong' if called else 'weak', 'openCall': 'down' if called else 'none',
                                   'closeCall': 'down' if called else 'none', 'topDriver': 'spyPct'},
                     'analog': None, 'actual': {'openPct': -0.5 if i < hits else 0.5, 'closePct': -0.5, 'intradayPct': 0.0,
                                                'source': 'naver_index_price'},
                     'result': {'openHit': (i < hits) if called else None, 'closeHit': True if called else None,
                                'openInBand': None}})
    return {'schemaVersion': mf.SCHEMA_VERSION, 'status': 'ok', 'ledger': rows, 'current': None}


def check_case8_scorecard() -> None:
    previous = make_rows(25, 8, 15, live_from=20)  # rows 20..24 live; calls in rows 0..14 only
    block = run('2026-09-01T07:00:00', previous)
    window = [r for r in block['ledger'] if r['state'] in ('open_scored', 'closed_scored', 'no_forecast')][-20:]
    recent = block['scorecard']['recent']
    assert len(recent['marks']) == 20 == recent['sessions'], recent
    assert recent['calls'] == sum(m in ('hit', 'miss') for m in recent['marks']) == sum((r['predicted'] or {}).get('openCall', 'none') != 'none' for r in window)
    assert recent['openHits'] == recent['marks'].count('hit') == sum((r['result'] or {}).get('openHit') is True for r in window)
    assert recent['liveSessions'] == sum(r['origin'] == 'live' for r in window) and recent['liveSessions'] > 0, recent
    live = block['scorecard']['live']
    assert live['since'] == previous['ledger'][20]['targetSession'] and live['calls'] == 0, live  # rows 20-24 are weak days
    assert block['scorecard']['backtest'] == {'from': '2024-11-06', 'to': '2026-11-06', 'days': 472, 'calls': 281,
                                              'openHits': 237, 'noCallDays': 191}, block['scorecard']['backtest']
    # Void rows are not part of the recent window.
    voided = deepcopy(previous)
    voided['ledger'][-1]['state'] = 'void'
    assert len(run('2026-09-01T07:00:00', voided)['scorecard']['recent']['marks']) == 20


def check_case9_ledger_size_and_backfill() -> None:
    first = run('2026-11-06T06:45:00')  # no previous public snapshot
    old = [r for r in first['ledger'] if r['origin'] == 'backfill']
    assert len(old) == 20 and first['ledger'][-1]['origin'] == 'live' and len(first['ledger']) == 21, len(first['ledger'])
    assert old[-1]['targetSession'] == '2026-11-05' and old[0]['targetSession'] == '2026-10-08', (old[0]['targetSession'], old[-1]['targetSession'])
    assert all(r['issuedAt'] is None for r in old) and first['scorecard']['recent']['liveSessions'] == 0 + (
        1 if first['ledger'][-1]['state'] in ('open_scored', 'closed_scored') else 0)
    assert first['current']['origin'] == 'live', 'a backfill row is never shown as the current forecast'
    # Past 60 rows the oldest go; a live scored row that falls out is carried into the live totals.
    previous = make_rows(62, 40, 50, live_from=0, before='2026-11-06')
    block = run('2026-11-06T06:45:00', previous)
    assert len(block['ledger']) == 60, len(block['ledger'])
    assert [r['targetSession'] for r in block['ledger']] == sorted(r['targetSession'] for r in block['ledger'])
    carried = block['scorecard']['live']['carried']
    dropped = previous['ledger'][:len(previous['ledger']) + 1 - 60]  # the new T row also joins the 62
    assert carried['calls'] > 0 and carried['since'] == previous['ledger'][0]['targetSession'], (carried, len(dropped))
    again = run('2026-11-06T08:00:00', block)
    assert again['scorecard']['live']['carried'] == carried, 'carried totals must not grow twice'
    assert len(again['ledger']) == 60


def check_case10_failures() -> None:
    # One input missing: no exception, no row, the rest of the snapshot is untouched.
    series, prices = view(at('2026-11-06T07:00:00'), drop=('EWY',))
    snapshot = {'signals': {'kospi': {'value': 1}}, 'news': {'items': [1]}}
    out = mf.apply_morning_forecast(snapshot, None, series, at('2026-11-06T07:00:00'), lambda url: [])
    assert out is snapshot and snapshot['signals'] == {'kospi': {'value': 1}} and snapshot['news'] == {'items': [1]}
    assert 'morningForecast' in snapshot and snapshot['morningForecast']['status'] in ('ok', 'unavailable')
    # The whole layer failing keeps the old ledger and says why; nothing else in the snapshot changes.
    good = run('2026-11-06T06:45:00')
    original = mf.compute_block
    try:
        def boom(*args, **kwargs):
            raise RuntimeError('provider changed')
        mf.compute_block = boom
        snapshot = {'signals': {'kospi': {'value': 2}}}
        mf.apply_morning_forecast(snapshot, {'morningForecast': good}, {}, at('2026-11-06T07:00:00'), lambda url: [])
    finally:
        mf.compute_block = original
    block = snapshot['morningForecast']
    assert block['status'] == 'unavailable' and block['reason'] == 'RuntimeError' and block['phase'] == 'off', block
    assert block['ledger'] == good['ledger'] and block['current'] == good['current'], 'ledger and card must survive'
    mf.assert_public_morning_forecast(block)
    assert snapshot['signals'] == {'kospi': {'value': 2}}
    # ...and the next good run continues from the preserved ledger (the frozen row is still frozen).
    resumed = run('2026-11-06T08:00:00', json.loads(json.dumps(block)))
    assert resumed['current']['issuedAt'] == good['current']['issuedAt'] and resumed['status'] == 'ok'
    # An unsupported KR calendar year is a public reason, not an exception.
    snapshot = {}
    mf.apply_morning_forecast(snapshot, None, view(at('2026-11-06T07:00:00'))[0], datetime(2029, 1, 3, 1, tzinfo=KST), lambda url: [])
    assert snapshot['morningForecast']['status'] == 'unavailable' and snapshot['morningForecast']['reason'] == 'kr_calendar_unsupported'
    # Garbage previous data never raises.
    for junk in ({'morningForecast': 'x'}, {'morningForecast': {'schemaVersion': mf.SCHEMA_VERSION, 'ledger': [1, None, {'targetSession': 'x'}]}}):
        mf.apply_morning_forecast({}, junk, view(at('2026-11-06T07:00:00'))[0], at('2026-11-06T07:00:00'), lambda url: [])


def check_pause() -> None:
    # 20 scored rows, 12 calls, 6 hits (50% < 60%): paused. 10 calls with 6 hits (60%): not paused.
    sick = run('2026-09-01T07:00:00', make_rows(20, 6, 12))
    assert sick['status'] == 'paused' and sick['current']['paused'] is True, sick['status']
    assert sick['current']['copy'] == {'title': {'ko': '최근 성적이 나빠 예보를 쉬어요', 'en': 'Forecast paused: recent record is weak'},
                                       'badge': {'ko': '예보 쉬는 중', 'en': 'Paused', 'tone': 'neutral'}}, sick['current']['copy']
    assert sick['current']['predicted'] is not None, 'a paused day is still computed and recorded'
    scored = run('2026-09-01T16:00:00', sick)
    assert row_of(scored, '2026-09-01')['paused'] is True and row_of(scored, '2026-09-01')['state'] == 'closed_scored'
    healthy = run('2026-09-01T07:00:00', make_rows(20, 6, 10))
    assert healthy['status'] == 'ok' and 'paused' not in healthy['current'], healthy['status']
    few = run('2026-09-01T07:00:00', make_rows(20, 1, 9))  # 9 calls only: not enough to pause
    assert few['status'] == 'ok'
    # Recovery: the paused row stays paused; the next forecast after the record recovers is not.
    mended = make_rows(20, 9, 12)
    mended['ledger'].append(row_of(sick, '2026-09-01'))
    assert mf.is_paused({r['targetSession']: r for r in mended['ledger']}) is False


# ---------------------------------------------------------------- copy rules (SPEC §6.5)

KO_BANNED = ('확인', '점검', '살펴', '지켜', '참고', '가능성도', '수 있어요', '다음 갱신')
KO_ACTION = ('사세요', '파세요', '담으세요', '비중', '목표가', '손절', '추천')
NAKED_TRADE = re.compile(r'(?<!순)(매수|매도)')   # SPEC §6.5 (2026-11-07): 순매수 / 순매도 are allowed
NAMES = ('삼성전자', 'SK하이닉스', '하이닉스', '엔비디아', '테슬라', '애플', 'NVIDIA', 'Tesla', 'Apple', 'Samsung', 'SPY', 'SOXX', 'EWY')
EN_BANNED = re.compile(r'\b(watch|monitor|may|could|buy|sell|sold)\b|(?<!net )\b(buying|selling)\b|keep an eye|recommend', re.I)
TITLE_KO = re.compile(r'^(오늘|[월화수목금토일]요일|\d+월 \d+일)'
                      r'( 한국장, (크게 밀려서 시작해요|내려서 시작할 가능성이 높아요|크게 올라서 시작해요|올라서 시작할 가능성이 높아요)'
                      r'|은 방향을 내지 않아요)$')
FIXED_TITLES_KO = {'밤사이 미국장이 쉬어서 예보가 없어요', '최근 성적이 나빠 예보를 쉬어요'}
DATA_TITLE_KO = re.compile(r'^자료가 덜 들어와 (오늘|[월화수목금토일]요일|\d+월 \d+일) 예보를 내지 못했어요$')


def strings_of(node, path=''):
    if isinstance(node, dict):
        if 'ko' in node and 'en' in node:
            yield path, 'ko', node['ko']
            yield path, 'en', node['en']
        for key, value in node.items():
            if key not in ('ko', 'en'):
                yield from strings_of(value, f'{path}.{key}')
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from strings_of(value, f'{path}[{i}]')


def title_fits(title: str, limit: int = 22) -> bool:
    """22 characters a line, one line break allowed (at the comma or a space)."""
    if len(title) <= limit:
        return True
    cuts = [i for i, ch in enumerate(title) if ch == ' ']
    return any(len(title[:i].rstrip(',')) <= limit and len(title[i + 1:]) <= limit for i in cuts)


def check_copy_rules(copy: dict, label: str) -> None:
    for path, lang, text in strings_of(copy):
        where = f'{label}{path}.{lang}: {text!r}'
        assert isinstance(text, str) and text, where
        assert '!' not in text and all(not (0x1F000 <= ord(c) or 0x2600 <= ord(c) <= 0x27BF) for c in text), f'no ! or emoji: {where}'
        if lang == 'ko':
            for word in KO_BANNED + KO_ACTION + NAMES:
                assert word not in text, f'banned {word}: {where}'
            assert not NAKED_TRADE.search(text), f'banned 매수/매도 without 순: {where}'
            assert '가능성이' not in text or path == '.title', f'가능성이 only in the title: {where}'
        else:
            assert not EN_BANNED.search(text), f'banned English: {where}'
            assert not any(n in text for n in NAMES), where
    title = copy['title']['ko']
    assert TITLE_KO.match(title) or title in FIXED_TITLES_KO or DATA_TITLE_KO.match(title), f'{label} title not in the table: {title}'
    assert title_fits(title), f'{label} title too long: {title}'
    sentences = [copy[k] for k in ('say', 'past') if k in copy]
    labels = [copy[k] for k in ('openRange', 'closeNote', 'openResult') if k in copy]
    labels += [c for c in copy.get('chips', [])]
    labels += [copy['openStat']] if 'openStat' in copy and copy['openStat']['ko'] != '보합권' else []
    for item in sentences + labels:
        for lang in ('ko', 'en'):
            assert re.search(r'\d', item[lang]), f'{label}: a number or time is required: {item[lang]!r}'
    for item in sentences:
        assert re.search(r'요(\.|\(\d+%\)\.|\. \(\d{4}년 \d{1,2}월부터\))$', item['ko']), f"{label}: 해요체: {item['ko']!r}"
        assert len(item['ko']) <= 80, item['ko']
    for chip in copy.get('chips', []):
        assert chip['tone'] in ('minus', 'plus', 'neutral') and len(chip['ko']) <= 24, chip
    if 'resultHeadline' in copy:  # a headline, like the title, carries no number; it is 해요체 and short
        assert copy['resultHeadline']['ko'].endswith('요') and len(copy['resultHeadline']['ko']) <= 22, copy['resultHeadline']
        assert 'resultBadge' in copy and copy['resultBadge']['tone'] in ('minus', 'plus', 'neutral') and len(copy['resultBadge']['ko']) <= 8
    assert ('resultHeadline' in copy) == ('resultBadge' in copy) == ('resultRows' in copy), 'result parts come together'


def synthetic_row(call: str, strength: str, state: str = 'issued', *, open_pct=None, close_pct=None, intraday=None,
                  band=(-2.0, -1.0), in_band=None, target='2026-11-06', paused=False, reason=None, origin='live',
                  close_call=None) -> dict:
    predicted = {'openPct': {'down': -1.5, 'up': 1.5, 'none': 0.1}[call] * (2 if strength == 'strong' and call != 'none' else 1)
                 if strength != 'moderate' or call == 'none' else {'down': -0.5, 'up': 0.5}[call],
                 'closePct': {'down': -1.0, 'up': 1.0, 'none': 0.1}[call], 'openBand': list(band), 'strength': strength,
                 'openCall': call, 'closeCall': close_call or call, 'topDriver': 'spyPct'}
    bucket = {'down': {'strong': 'open_le_-1.0', 'moderate': 'open_-0.6_-0.3'}, 'up': {'strong': 'open_ge_1.0', 'moderate': 'open_0.3_0.6'}}
    analog = mf.analog_record(call, bucket.get(call, {}).get(strength), FULL_MODEL) if call != 'none' else None
    row = {'targetSession': target, 'basisKrSession': '2026-11-05', 'usSessions': ['2026-11-05'], 'origin': origin, 'state': state,
           'issuedAt': '2026-11-05T21:45:00Z', 'noForecastReason': reason, 'inputs': {'spyPct': 0.3, 'soxxPct': -0.2, 'ewyPct': 1.0,
                                                                                    'kospiPrevPct': 0.4, 'ewyExcessPct': 0.6},
           'predicted': predicted, 'analog': analog, 'actual': None, 'result': None}
    if state in ('open_scored', 'closed_scored'):
        row['actual'] = {'openPct': open_pct, 'closePct': close_pct, 'intradayPct': intraday, 'source': 'naver_index_price'}
        row['result'] = {'openHit': mf.call_hit(call, open_pct), 'closeHit': mf.call_hit(row['predicted']['closeCall'], close_pct) if close_pct is not None else None,
                         'openInBand': in_band}
    if paused:
        row['paused'] = True
    if state == 'no_forecast':
        row.update(predicted=None, analog=None, inputs=None)
    return row


def check_case11_copy() -> None:
    today = date(2026, 11, 6)
    seen = set()
    models = (FULL_MODEL, mf.load_model())
    for model in models:
        for target in ('2026-11-06', '2026-11-09', '2026-11-03'):   # today, a later weekday, a past day
            cases = [('A strong down', synthetic_row('down', 'strong')), ('A strong up', synthetic_row('up', 'strong')),
                     ('A moderate down', synthetic_row('down', 'moderate')), ('A moderate up', synthetic_row('up', 'moderate')),
                     ('B none', synthetic_row('none', 'weak')),
                     ('A close none', synthetic_row('down', 'strong', close_call='none')),
                     ('C open hit', synthetic_row('down', 'strong', 'open_scored', open_pct=-2.2, in_band=True)),
                     ('C open miss', synthetic_row('down', 'strong', 'open_scored', open_pct=0.4, in_band=False)),
                     ('D H7', synthetic_row('down', 'strong', 'closed_scored', open_pct=-1.5, close_pct=-3.0, intraday=-1.5, in_band=True)),
                     ('D H5 further', synthetic_row('down', 'strong', 'closed_scored', open_pct=-2.2, close_pct=-6.0, intraday=-3.9, in_band=False)),
                     ('D H5 reversed', synthetic_row('down', 'strong', 'closed_scored', open_pct=-2.2, close_pct=-1.0, intraday=1.2, in_band=False)),
                     ('D H5 up', synthetic_row('up', 'strong', 'closed_scored', open_pct=2.2, close_pct=3.0, intraday=0.8, in_band=False)),
                     ('D H6', synthetic_row('down', 'strong', 'closed_scored', open_pct=0.4, close_pct=0.2, intraday=-0.2, in_band=False)),
                     ('D none', synthetic_row('none', 'weak', 'closed_scored', open_pct=-0.4, close_pct=-0.6, intraday=-0.2)),
                     ('E closed', synthetic_row('none', 'weak', 'no_forecast', reason='us_market_closed')),
                     ('E late', synthetic_row('none', 'weak', 'no_forecast', reason='missed_window')),
                     ('E suspect', synthetic_row('none', 'weak', 'no_forecast', reason='inputs_suspect')),
                     ('F paused', synthetic_row('down', 'strong', paused=True))]
            for name, row in cases:
                row['targetSession'] = target
                copy = mf.build_copy(row, today, model)
                check_copy_rules(copy, f'{name}/{target}')
                if 'say' in copy:
                    seen.add(copy['say']['id'])
    assert seen == {'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'H7'}, seen
    # The numbers are the model file's. Fixed model first: SPEC §4.5 examples.
    say = lambda row, model=FULL_MODEL: mf.build_copy(row, today, model)['say']
    assert say(synthetic_row('down', 'strong'))['ko'] == '급락 출발 뒤 장중 반등은 과거 46%, 반반이에요. 장이 열린 뒤 방향은 예보하지 않아요.'
    assert '53%' in say(synthetic_row('up', 'strong'))['ko'] and '반반이에요' in say(synthetic_row('up', 'strong'))['ko']
    assert say(synthetic_row('down', 'moderate'))['ko'] == '이 정도 신호면 시가 방향은 84% 맞았어요. 종가까지는 덜 맞아요(71%).'
    assert say(synthetic_row('none', 'weak'))['ko'] == '신호가 이 정도로 약한 날은 종가 방향이 47%만 맞았어요. 맞히는 척하지 않을게요.'
    h5 = synthetic_row('down', 'strong', 'closed_scored', open_pct=-2.2, close_pct=-6.0, intraday=-3.9, in_band=False)
    assert say(h5)['ko'] == '장중에 3.9% 더 빠졌어요. 장중 움직임은 제가 예보하지 못하는 부분이에요.'
    h7 = synthetic_row('down', 'strong', 'closed_scored', open_pct=-2.2, close_pct=-6.0, intraday=-3.9, in_band=True)
    assert say(h7)['ko'] == '오늘은 예보대로였어요. 시가 −2.2%, 예상 범위 안이에요.'
    h6 = synthetic_row('down', 'strong', 'closed_scored', open_pct=0.4, close_pct=0.2, intraday=-0.2)
    assert say(h6)['id'] == 'H6' and '+0.4%' in say(h6)['ko']
    # H6 and H7 name the day like the title does (1-C follow-up): not "오늘" when the target day is not today.
    def say_on(row: dict, target: str, on: date) -> dict:
        row['targetSession'] = target
        return mf.build_copy(row, on, FULL_MODEL)['say']
    h7_row = lambda: synthetic_row('down', 'strong', 'closed_scored', open_pct=-2.2, close_pct=-6.0, intraday=-3.9, in_band=True)
    h6_row = lambda: synthetic_row('down', 'strong', 'closed_scored', open_pct=0.4, close_pct=0.2, intraday=-0.2)
    saturday, sunday = date(2026, 11, 7), date(2026, 11, 8)
    assert say_on(h7_row(), '2026-11-06', saturday)['ko'] == '11월 6일은 예보대로였어요. 시가 −2.2%, 예상 범위 안이에요.'  # Friday seen on Saturday
    assert say_on(h7_row(), '2026-11-06', saturday)['en'] == 'Nov 6 went as forecast. The open was −2.2%, inside the expected range.'
    assert say_on(h7_row(), '2026-11-06', date(2026, 11, 6))['ko'].startswith('오늘은 예보대로였어요.')
    assert say_on(h7_row(), '2026-11-06', date(2026, 11, 6))['en'].startswith('Today went as forecast.')
    assert say_on(h7_row(), '2026-11-09', sunday)['ko'].startswith('월요일은 예보대로였어요.')  # a later day: the weekday
    assert say_on(h7_row(), '2026-11-09', sunday)['en'].startswith('Monday went as forecast.')
    assert say_on(h6_row(), '2026-11-06', saturday)['ko'].startswith('11월 6일은 틀렸어요. 밤사이 신호와 반대로 시가 +0.4%로 시작했어요.')
    assert say_on(h6_row(), '2026-11-06', saturday)['en'].startswith('I was wrong on Nov 6. The market opened +0.4%')
    assert say_on(h6_row(), '2026-11-06', date(2026, 11, 6))['ko'].startswith('오늘은 틀렸어요.')
    assert say_on(h6_row(), '2026-11-06', date(2026, 11, 6))['en'].startswith('I was wrong today.')
    assert say_on(h6_row(), '2026-11-09', sunday)['ko'].startswith('월요일은 틀렸어요.')
    assert say_on(h6_row(), '2026-11-09', sunday)['en'].startswith('I was wrong on Monday.')
    for row_call, on_day in ((h7_row, saturday), (h6_row, saturday), (h7_row, sunday)):  # the new wording passes the copy rules
        row = row_call()
        row['targetSession'] = '2026-11-06'
        check_copy_rules(mf.build_copy(row, on_day, FULL_MODEL), f'say_after on {on_day}')
    # Real model file: every number in H1-H4 and the past line is derived from it, none typed here.
    real = mf.load_model()
    rates = real['baseRates']
    down_rate = round(100 * rates['gapDownIntradayRebound']['rebounded'] / rates['gapDownIntradayRebound']['days'])
    assert f'{down_rate}%' in say(synthetic_row('down', 'strong'), real)['ko']
    assert f"{rates['moderateOpenHitPct']}%" in say(synthetic_row('down', 'moderate'), real)['ko']
    assert f"{rates['moderateCloseHitPct']}%" in say(synthetic_row('down', 'moderate'), real)['ko']
    assert f"{rates['weakCloseHitPct']}%" in say(synthetic_row('none', 'weak'), real)['ko']
    # 2026-11-06 (SPEC examples): the numbers a reader sees.
    copy = run('2026-11-06T06:45:00')['current']['copy']
    assert copy['title'] == {'ko': '오늘 한국장, 크게 밀려서 시작해요', 'en': 'Korea is set to open sharply lower'}
    assert copy['openStat']['ko'] == '−3% 안팎' and copy['openRange']['ko'] == '예상 범위 −2 ~ −4.5%', copy
    assert copy['closeStat']['ko'] == '하락 쪽' and copy['closeNote']['ko'] == '비슷한 36일 중 28일', copy
    assert [c['ko'] for c in copy['chips']] == ['S&P500 −4.4%', '미국 반도체 −12.2%', '한국 ETF 코스피 대비 −8.2%p'], copy['chips']
    assert [c['en'] for c in copy['chips']] == ['S&P 500 −4.4%', 'US chips −12.2%', 'Korea ETF vs KOSPI −8.2pp'], copy['chips']
    assert [c['tone'] for c in copy['chips']] == ['minus', 'minus', 'minus']
    # The third chip follows the excess over KOSPI, not the raw ETF move: +5.4% raw but -0.3 beyond KOSPI is neutral.
    chips = mf.chips_copy({'spyPct': 0.3, 'soxxPct': 2.1, 'ewyPct': 5.4, 'kospiPrevPct': 5.7, 'ewyExcessPct': -0.3})
    assert chips[2] == {'ko': '한국 ETF 코스피 대비 −0.3%p', 'en': 'Korea ETF vs KOSPI −0.3pp', 'tone': 'neutral'}, chips
    assert mf.chips_copy({'spyPct': 0, 'soxxPct': 0, 'ewyPct': 0, 'kospiPrevPct': 0, 'ewyExcessPct': 6.0})[2]['tone'] == 'plus'
    assert copy['past']['ko'] == '밤사이 신호가 이만큼 나빴던 36일 중 33일은 하락 출발했어요. (2024년 11월부터)', copy['past']
    assert copy['badge'] == {'ko': '신호 뚜렷', 'en': 'Strong signal', 'tone': 'minus'}
    check_copy_rules(copy, 'live 2026-11-06 06:45')
    # After the close: the result table.
    done = run('2026-11-06T15:40:00', run('2026-11-06T06:45:00'))['current']['copy']
    check_copy_rules(done, 'live 2026-11-06 15:40')
    assert done['say']['id'] == 'H7' and done['resultTitle']['ko'] == '오늘 예보 결과', done  # -2.24 is inside -4.37..-2.07
    assert done['resultHeadline']['ko'] == '오늘은 예보대로였어요' and done['resultBadge']['ko'] == '방향 적중', done
    assert [r['key'] for r in done['resultRows']] == ['open', 'open_size', 'close', 'close_size']
    assert [r['hit'] for r in done['resultRows']] == [True, True, True, None]
    # Titles for every table row, both languages (SPEC §4.3), with the weekday swap.
    def title(call, strength, target):
        return mf.title_copy(synthetic_row(call, strength, target=target), today)
    assert title('down', 'strong', '2026-11-06') == {'ko': '오늘 한국장, 크게 밀려서 시작해요', 'en': 'Korea is set to open sharply lower'}
    assert title('down', 'moderate', '2026-11-06')['ko'] == '오늘 한국장, 내려서 시작할 가능성이 높아요'
    assert title('none', 'weak', '2026-11-06') == {'ko': '오늘은 방향을 내지 않아요', 'en': 'No call today'}
    assert title('up', 'moderate', '2026-11-06')['en'] == 'Korea is likely to open higher'
    assert title('up', 'strong', '2026-11-06') == {'ko': '오늘 한국장, 크게 올라서 시작해요', 'en': 'Korea is set to open sharply higher'}
    assert title('down', 'strong', '2026-11-09')['ko'] == '월요일 한국장, 크게 밀려서 시작해요'
    assert title('none', 'weak', '2026-11-09')['ko'] == '월요일은 방향을 내지 않아요'
    assert mf.title_copy(synthetic_row('none', 'weak', 'no_forecast', reason='missed_window'), today)['en'] == 'No forecast: data arrived too late'
    # Numbers: 0.5 steps, flat band, no "-0".
    assert mf.fmt_half(-3.22) == '−3' and mf.fmt_half(-3.75) == '−4' and mf.fmt_half(3.75, True) == '+4' and mf.fmt_half(-0.2) == '0'
    assert mf.size_copy(0.4)['ko'] == '보합권' and mf.size_copy(-3.7)['ko'] == '−3.5% 안팎' and mf.fmt_pct(-0.04) == '0.0%'
    assert mf.fmt_num(-1358.4, 0) == '−1,358'
    # Result headline and badge, only once the close is scored (SPEC follow-up from review).
    def head(row, target='2026-11-06'):
        row['targetSession'] = target
        copy = mf.build_copy(row, today, FULL_MODEL)
        return copy.get('resultHeadline'), copy.get('resultBadge')
    hit_in = synthetic_row('down', 'strong', 'closed_scored', open_pct=-2.2, close_pct=-6.0, intraday=-3.9, in_band=True)
    hit_out = synthetic_row('down', 'strong', 'closed_scored', open_pct=-2.2, close_pct=-6.0, intraday=-3.9, in_band=False)
    wrong = synthetic_row('down', 'strong', 'closed_scored', open_pct=0.4, close_pct=0.2, intraday=-0.2, in_band=False)
    nocall = synthetic_row('none', 'weak', 'closed_scored', open_pct=-0.4, close_pct=-0.6, intraday=-0.2)
    assert head(hit_in) == ({'ko': '오늘은 예보대로였어요', 'en': 'Today went as forecast'}, {'ko': '방향 적중', 'en': 'Direction right', 'tone': 'plus'})
    assert head(hit_out) == ({'ko': '방향은 맞았고, 크기는 빗나갔어요', 'en': 'Direction was right, size was off'},
                             {'ko': '방향 적중', 'en': 'Direction right', 'tone': 'plus'})
    assert head(wrong) == ({'ko': '오늘 예보는 빗나갔어요', 'en': "Today's call was wrong"}, {'ko': '빗나감', 'en': 'Missed', 'tone': 'minus'})
    assert head(nocall) == ({'ko': '오늘은 방향을 내지 않았어요', 'en': 'No call today'}, {'ko': '방향 없음', 'en': 'No call', 'tone': 'neutral'})
    assert head(wrong, '2026-11-03')[0] == {'ko': '11월 3일 예보는 빗나갔어요', 'en': "Nov 3's call was wrong"}
    assert head(hit_in, '2026-11-03')[0]['ko'] == '11월 3일은 예보대로였어요'
    assert head(nocall, '2026-11-03')[0]['ko'] == '11월 3일은 방향을 내지 않았어요'
    for partial in (synthetic_row('down', 'strong', 'open_scored', open_pct=-2.2, in_band=True), synthetic_row('down', 'strong')):
        assert head(partial) == (None, None), 'no headline while only the open is scored'
    # The analog widens toward zero below 20 days and drops out when it still has fewer.
    thin = deepcopy(FULL_MODEL)
    thin['analogBuckets'][1].update(days=4, openDown=3, closeDown=2)
    thin['analogBuckets'][0].update(days=12, openDown=10, closeDown=8)
    wide = mf.analog_record('down', 'open_le_-1.0', thin)
    assert wide['days'] == 12 + 4 + 12 and wide['widened'] is True and wide['openSame'] == 10 + 3 + 8, wide
    for bucket in thin['analogBuckets']:
        bucket['days'] = 2
    assert mf.analog_record('down', 'open_le_-1.0', thin)['days'] == 6
    assert 'past' not in mf.build_copy(dict(synthetic_row('down', 'strong'), analog=mf.analog_record('down', 'open_le_-1.0', thin)), today, thin)


def check_public_shape() -> None:
    block = run('2026-11-06T16:00:00', run('2026-11-06T06:45:00'))
    assert list(block) == ['schemaVersion', 'modelVersion', 'generatedAt', 'status', 'phase', 'current', 'ledger', 'scorecard',
                           'nextIssueAt', 'disclaimer'], list(block)
    assert all('copy' not in r and 'inputs' not in r for r in block['ledger'])
    assert 'copy' in block['current'] and 'inputs' in block['current']
    assert block['disclaimer']['ko'] == '과거 기록이며 앞으로의 결과를 보장하지 않습니다. 투자 권유가 아닙니다.'
    raw = json.dumps(block, ensure_ascii=False).lower()
    assert not any(token in raw for token in ('apikey', 'secret', 'password', 'token', '/users/', 'naver.com', 'yahoo.com')), 'public block must stay clean'
    # Old public data without the block passes the pre-publish check; a present block is still judged.
    for old_data in ({}, {'morningForecast': None}):
        mf.assert_public_forecast_if_present(old_data, {})
        mf.assert_public_forecast_if_present(old_data, {'morningForecastStatus': 'ok'})
    mf.assert_public_forecast_if_present({'morningForecast': block}, {'morningForecastStatus': block['status']})
    for args in (({'morningForecast': {**block, 'schemaVersion': 'x'}}, None),
                 ({'morningForecast': block}, {'morningForecastStatus': 'unavailable'})):
        try:
            mf.assert_public_forecast_if_present(*args)
        except AssertionError:
            continue
        raise AssertionError('a present block must still be checked')
    for broken in ({**block, 'schemaVersion': 'x'}, {**block, 'status': 'weird'}, {**block, 'ledger': 'x'},
                   {**block, 'scorecard': {'recent': {'marks': ['hit'] * 21}}}):
        try:
            mf.assert_public_morning_forecast(broken)
        except AssertionError:
            continue
        raise AssertionError('the public-shape check accepted a broken block')


def check_readers_and_entry_point() -> None:
    import polarmeter_v3_inputs as inputs
    # Naver rows: strings with commas, newest first, a zero price is "no price".
    rows = [{'localTradedAt': '2026-11-06', 'closePrice': '5,059.69', 'openPrice': '5,263.26'},
            {'localTradedAt': '2026-11-05', 'closePrice': '5,383.80', 'openPrice': '0'}, {'bad': 1}, 'x']
    parsed = inputs.naver_kospi_prices(rows)
    assert parsed == {'2026-11-06': {'open': 5263.26, 'close': 5059.69, 'source': 'naver_index_price'},
                      '2026-11-05': {'open': None, 'close': 5383.8, 'source': 'naver_index_price'}}, parsed
    assert inputs.naver_kospi_prices({'not': 'a list'}) == {}
    # Yahoo is only the fallback; an open equal to the previous close is the known bad value and is dropped.
    stamps = [1793840400 + 86400 * i for i in range(3)]  # 2026-11-05 KST 09:00 and the next two days
    payload = {'chart': {'result': [{'meta': {'exchangeTimezoneName': 'Asia/Seoul'}, 'timestamp': stamps,
                                     'indicators': {'quote': [{'open': [100.0, 100.0, 90.0], 'close': [100.0, 95.0, 90.5]}]}}]}}
    yahoo = inputs.yahoo_kospi_prices(payload)
    assert [r['open'] for r in yahoo.values()] == [100.0, None, 90.0] and all(r['source'] == 'yahoo_chart_daily' for r in yahoo.values())

    def only_yahoo(url: str):
        if urlparse(url).hostname == 'm.stock.naver.com':
            raise OSError('naver down')
        return payload
    assert inputs.fetch_kospi_daily(only_yahoo) == yahoo
    assert inputs.fetch_kospi_daily(lambda url: (_ for _ in ()).throw(OSError('all down'))) == {}
    # The entry point end to end on a fake network: Naver rows from the fixed world, newest first.
    moment = at('2026-11-06T09:05:00')
    series, prices = view(moment)
    naver = [{'localTradedAt': d, 'closePrice': f"{r['close']:,.2f}", 'openPrice': f"{r['open']:,.2f}"}
             for d, r in sorted(prices.items(), reverse=True)][:60]
    urls = []

    def fake(url: str):
        urls.append(url)
        if urlparse(url).hostname == 'm.stock.naver.com' and urlparse(url).path == '/api/index/KOSPI/price':
            return naver
        raise OSError('unexpected ' + url)
    snapshot = {'signals': {'kospi': {'value': 1}}}
    issued = mf.apply_morning_forecast(dict(snapshot), None, series, at('2026-11-06T06:50:00'), fake)['morningForecast']
    assert issued['status'] == 'ok' and issued['current']['state'] == 'issued', issued['status']
    out = mf.apply_morning_forecast(snapshot, {'morningForecast': issued}, series, moment, fake)
    block = out['morningForecast']
    assert out is snapshot and block['current']['state'] == 'open_scored' and block['current']['actual']['source'] == 'naver_index_price'
    assert len(urls) == 2 and all('pageSize=60' in u and 'page=1' in u for u in urls), urls  # one Naver call a run, Yahoo untouched
    # apply_v3_layers hands over the series it already fetched (and still works when everything is down).
    assert 'morningForecast' in inputs.apply_v3_layers({}, None, fetch=lambda url: (_ for _ in ()).throw(OSError('down')), pause=0)


def check_data_outage_rules() -> None:
    previous = run('2026-11-06T06:45:00')
    # Everything down: unavailable, the old block's ledger and card ride along.
    series, _ = view(at('2026-11-06T09:05:00'), drop=('SPY', 'SOXX', 'EWY'))
    out = mf.apply_morning_forecast({}, {'morningForecast': previous}, {}, at('2026-11-06T09:05:00'), lambda url: (_ for _ in ()).throw(OSError('down')))
    block = out['morningForecast']
    assert block['status'] == 'unavailable' and block['reason'] == 'market_data_unavailable' and block['ledger'] == previous['ledger']
    # Same outage with no previous snapshot: no junk first ledger is written.
    nothing = mf.apply_morning_forecast({}, None, {}, at('2026-11-06T07:00:00'), lambda url: (_ for _ in ()).throw(OSError('down')))['morningForecast']
    assert nothing['status'] == 'unavailable' and nothing['ledger'] == [] and nothing['current'] is None
    # One US input missing on a normal run: no row, no exception, scoring of what is already issued goes on.
    held = run('2026-11-06T09:05:00', previous, drop=('EWY',))
    assert held['status'] == 'ok' and row_of(held, '2026-11-06')['state'] == 'open_scored'
    waiting = run('2026-11-09T07:30:00', held, drop=('EWY',))
    assert not any(r['targetSession'] == '2026-11-09' for r in waiting['ledger']) and waiting['status'] == 'ok'
    # The freeze rule is also enforced in code: a mutation of an issued row turns the block into 'unavailable'.
    original = mf.score_row
    try:
        def mutate(row, prices, now):
            original(row, prices, now)
            if row['state'] != 'pending' and row.get('predicted'):
                row['predicted'] = dict(row['predicted'], openPct=9.99)
        mf.score_row = mutate
        out = mf.apply_morning_forecast({}, {'morningForecast': previous}, view(at('2026-11-06T09:05:00'))[0], at('2026-11-06T09:05:00'),
                                        lambda url: [{'localTradedAt': d, 'closePrice': str(r['close']), 'openPrice': str(r['open'])}
                                                     for d, r in view(at('2026-11-06T09:05:00'))[1].items()])
    finally:
        mf.score_row = original
    assert out['morningForecast']['status'] == 'unavailable' and out['morningForecast']['reason'] == 'frozen_row_changed', out['morningForecast']['reason']
    assert out['morningForecast']['ledger'] == previous['ledger']


def main() -> None:
    check_20261106()
    check_weekday()
    check_friday_to_monday()
    check_kr_only_holiday()
    check_us_holiday()
    check_after_us_only_holiday()
    check_broken_inputs()
    check_calls_and_buckets()
    check_case1_hand_calculation()
    check_case2_freeze()
    check_case3_no_issue_while_us_trades()
    check_case4_missed_window()
    check_case5_holidays()
    check_case6_friday_to_monday()
    check_case7_scoring()
    check_case8_scorecard()
    check_case9_ledger_size_and_backfill()
    check_case10_failures()
    check_pause()
    check_case11_copy()
    check_public_shape()
    check_readers_and_entry_point()
    check_data_outage_rules()
    print('PASS morning forecast: inputs (1-A) + freeze, window, scoring, scorecard, ledger, pause, failure, copy rules and public shape (1-B)')


if __name__ == '__main__':
    main()
