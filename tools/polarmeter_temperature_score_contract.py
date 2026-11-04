#!/usr/bin/env python3
"""Offline contract: public score core and history must share one calculation."""
from __future__ import annotations

from copy import deepcopy

from polarmeter_free_cache_worker import (
    apply_temperature_history,
    assert_public_payload_safe,
    public_temperature_score_core,
    sanitize_public_snapshot,
    temperature_history_entry,
)


def signal(value: float, change_pct: float, status: str = 'ok') -> dict:
    return {
        'value': value,
        'changePct': change_pct,
        'status': status,
        'dataAsOf': '2026-09-09T05:00:00Z',
        'fetchedAt': '2026-09-09T05:00:00Z',
    }


def snapshot(*, generated_at: str = '2026-09-09T05:00:00Z', session: str | None = None, status: str = 'ok') -> dict:
    values = {
        'sp500': signal(6200, 0.4, status),
        'nasdaq100': signal(22000, 0.6, status),
        'iwm': signal(214, 0.2, status),
        'vix': signal(16, -1.0, status),
        'us10y': signal(4.2, -0.1, status),
        'dxy': signal(102, -0.1, status),
        'usd_krw': signal(1380, -0.2, status),
        'kospi': signal(2750, 0.3, status),
        'kosdaq': signal(850, 0.2, status),
        'kr_samsung': signal(92000, 0.4, status),
        'kr_hynix': signal(350000, 0.5, status),
        'soxx': signal(615, 0.7, status),
        'smh': signal(305, 0.6, status),
    }
    out = {'generatedAt': generated_at, 'signals': values}
    if session:
        out['market_session_type'] = session
    return out


def assert_same_score(snapshot_value: dict, expected_session: str) -> tuple[dict, dict]:
    core = public_temperature_score_core(snapshot_value)
    history = temperature_history_entry(snapshot_value)
    assert core is not None and history is not None
    assert core['formulaVersion'] == 'score_core_v2_normal_freshness'
    assert core['calculationProfile'] == 'normal'
    assert core['marketSession'] == expected_session
    assert history['sessionType'] == expected_session
    assert history['asOf'] == core['generatedAt'] == snapshot_value['generatedAt']
    assert history['usScore'] == round(core['us']['score'])
    assert history['krScore'] == round(core['kr']['score'])
    assert history['usLabel'] == core['us']['label']
    assert history['krLabel'] == core['kr']['label']
    assert history['source'] == 'score_core_v2_normal_freshness'
    assert_public_payload_safe({'scoreCore': core, 'temperatureHistory': {'items': [history]}})
    published = sanitize_public_snapshot(snapshot_value)
    assert published['scoreCore'] == core
    assert_public_payload_safe(published)
    return core, history


def with_history(item: dict, closes: list[tuple[str, float]]) -> dict:
    return {**item, 'history': {'points': [{'timestamp': f'{day}T00:00:00Z', 'value': value} for day, value in closes]}}


def check_kr_session_consistency() -> None:
    # 2026-11-03 23:41 KST: KR closed after a broad sell-off, US open 11 minutes.
    base = snapshot(generated_at='2026-11-03T14:41:05Z')
    base['signals']['kospi'] = signal(5648.79, -3.59)
    base['signals']['kosdaq'] = signal(704.39, -4.08)
    base['signals']['usd_krw'] = signal(1343.28, 1.34)
    base['signals']['kr_samsung'] = {**signal(229000, 1.78), 'dataAsOf': '20261102'}
    base['signals'].pop('kr_hynix')
    base['signals']['soxx'] = with_history(signal(467.98, 3.16), [('2026-10-30', 471.51), ('2026-11-02', 453.65), ('2026-11-03', 467.98)])
    base['signals']['smh'] = with_history(signal(521.47, 2.59), [('2026-10-30', 522.49), ('2026-11-02', 508.31), ('2026-11-03', 521.47)])
    for item in base['signals'].values():
        item.setdefault('dataAsOf', '2026-11-03T14:40:00Z')
        if item['dataAsOf'].startswith('2026-09'):
            item['dataAsOf'] = item['fetchedAt'] = '2026-11-03T14:40:00Z'
    core = public_temperature_score_core(base)
    kr = core['kr']
    # Tonight's US semis rally is not today's KR industry input.
    assert kr['components']['industry'] < 50, kr
    # A shared KOSPI/KOSDAQ sell-off is never shown as neutral.
    assert kr['score'] <= 35 and kr['label'].startswith('냉각'), kr
    assert kr['rawScore'] >= kr['score'], kr
    # US keeps its own live session inputs.
    assert core['us']['components']['sector'] > 50, core['us']

    rally = deepcopy(base)
    rally['signals']['kospi'] = signal(5900, 3.2)
    rally['signals']['kosdaq'] = signal(760, 3.4)
    for key in ('kospi', 'kosdaq'):
        rally['signals'][key]['dataAsOf'] = rally['signals'][key]['fetchedAt'] = '2026-11-03T14:40:00Z'
    assert public_temperature_score_core(rally)['kr']['score'] >= 65


def check_settled_history() -> None:
    def entry(generated_at: str, kospi_pct: float) -> dict:
        value = snapshot(generated_at=generated_at)
        for item in value['signals'].values():
            item['dataAsOf'] = item['fetchedAt'] = generated_at
        value['signals']['kospi'] = {**signal(2750, kospi_pct), 'dataAsOf': generated_at, 'fetchedAt': generated_at}
        value['signals']['kosdaq'] = {**signal(850, kospi_pct), 'dataAsOf': generated_at, 'fetchedAt': generated_at}
        return value

    settled = entry('2026-11-04T07:00:00Z', -2.0)   # 16:00 KST: KR closed, US pre-open
    intraday = entry('2026-11-05T01:00:00Z', 2.5)   # 10:00 KST next day: KR open
    first = apply_temperature_history(deepcopy(settled), {})
    history = apply_temperature_history(deepcopy(intraday), first)['temperatureHistory']
    by_date = {item['dateKst']: item for item in history['items']}
    # The open KR session carries the last settled close into today's row.
    assert by_date['2026-11-05']['krScore'] == by_date['2026-11-04']['krScore'], by_date
    assert by_date['2026-11-05']['usSettled'] is True
    # The live delta still compares the live temperature with the settled day.
    live_kr = public_temperature_score_core(intraday)['kr']['score']
    assert history['dailyDelta']['kr']['currentScore'] == live_kr
    assert history['dailyDelta']['kr']['previousScore'] == by_date['2026-11-04']['krScore']


def main() -> None:
    current = snapshot()
    current_core, current_history = assert_same_score(current, 'normal')
    assert current_history['dateKst'] == '2026-09-09'

    opposite = deepcopy(current)
    for key in ('sp500', 'nasdaq100', 'iwm', 'kospi', 'kosdaq', 'kr_samsung', 'kr_hynix', 'soxx', 'smh'):
        opposite['signals'][key]['changePct'] *= -8
    opposite['signals']['vix']['changePct'] = 18
    opposite['signals']['us10y']['changePct'] = 2.5
    opposite['signals']['usd_krw']['changePct'] = 2.2
    opposite_core, _ = assert_same_score(opposite, 'normal')
    assert opposite_core['us']['score'] < current_core['us']['score']
    assert opposite_core['kr']['score'] < current_core['kr']['score']

    missing = {'generatedAt': current['generatedAt'], 'signals': {}}
    assert public_temperature_score_core(missing) is None
    assert temperature_history_entry(missing) is None

    holiday = snapshot(generated_at='2026-09-09T05:00:00Z', session='holiday_kr')
    _, holiday_history = assert_same_score(holiday, 'holiday_kr')
    assert holiday_history['dateKst'] == '2026-09-09'

    delayed = snapshot(status='stale')
    delayed_core, _ = assert_same_score(delayed, 'normal')
    assert delayed_core['us']['score'] == current_core['us']['score']
    assert delayed_core['kr']['score'] == current_core['kr']['score']

    expired = deepcopy(current)
    expired['signals']['kr_samsung'].update(status='stale', dataAgeHours=41 * 24, changePct=-20)
    expired_core, _ = assert_same_score(expired, 'normal')
    absent = deepcopy(current)
    del absent['signals']['kr_samsung']
    absent_core, _ = assert_same_score(absent, 'normal')
    last_close = deepcopy(expired)
    last_close['signals']['kr_samsung'].update(status='ok', dataAgeHours=0)
    last_close_core, _ = assert_same_score(last_close, 'normal')
    assert expired_core['kr']['score'] == last_close_core['kr']['score']
    assert expired_core['kr']['components'] == last_close_core['kr']['components']
    assert expired_core['kr']['components'] != absent_core['kr']['components']
    for observed_at in ('2026-09-10T05:00:00Z', '2026-09-09T05:01:00Z'):
        future = deepcopy(expired)
        future['signals']['kr_samsung']['dataAsOf'] = observed_at
        future_core, _ = assert_same_score(future, 'normal')
        assert future_core['kr']['components'] == absent_core['kr']['components']

    check_kr_session_consistency()
    check_settled_history()
    print('PolarMeter temperature score contract: PASS')


if __name__ == '__main__':
    main()
