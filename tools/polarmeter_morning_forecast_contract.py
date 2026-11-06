#!/usr/bin/env python3
"""Offline contract: morning forecast inputs and prediction (SPEC §6.3, §6.6).

Bundle 1-A covers only the inputs and the prediction. Bundle 1-B appends the
freeze, window, scoring, scorecard, ledger and copy cases (SPEC §6.6 items
2-4 and 7-11) to this same file.

  PYTHONPATH=tools python3 tools/polarmeter_morning_forecast_contract.py
"""
from __future__ import annotations

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


def main() -> None:
    check_20261106()
    check_weekday()
    check_friday_to_monday()
    check_kr_only_holiday()
    check_us_holiday()
    check_after_us_only_holiday()
    check_broken_inputs()
    check_calls_and_buckets()
    print('PASS morning forecast 1-A: inputs (weekday, Fri->Mon, KR-only holiday, US holiday, after US-only holiday, '
          '2026-11-06) and prediction')


if __name__ == '__main__':
    main()
