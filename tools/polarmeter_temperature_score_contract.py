#!/usr/bin/env python3
"""Offline contract: public score core and history must share one calculation."""
from __future__ import annotations

from copy import deepcopy

from polarmeter_free_cache_worker import (
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
    assert expired_core['kr']['score'] == absent_core['kr']['score']
    assert expired_core['kr']['components'] == absent_core['kr']['components']

    print('PolarMeter temperature score contract: PASS')


if __name__ == '__main__':
    main()
