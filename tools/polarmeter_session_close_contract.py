"""Offline KR/US session-close retention and cross-client fixture contract."""
from copy import deepcopy
from datetime import datetime
import json
import sys
from unittest.mock import patch

import polarmeter_cache_snapshot as cache
import polarmeter_last_close_policy as policy
from polarmeter_market_calendar import last_completed_session_date
from polarmeter_free_cache_worker import (
    assert_public_payload_safe, public_temperature_score_core, sanitize_public_signal,
    sanitize_public_snapshot, snapshot_score_point,
)
from polarmeter_temperature_score_contract import snapshot


CASES = [
    ('kr_new_year_holiday', 'kospi', '2026-02-16T12:00:00+09:00', '20260213', '2026-02-13', 'closed'),
    ('kr_long_holiday', 'kospi', '2026-02-18T16:44:59+09:00', '20260213', '2026-02-13', 'closed'),
    ('kr_before_reopen', 'kosdaq', '2026-02-19T07:00:00+09:00', '20260213', '2026-02-13', 'pre_open'),
    ('kr_reopen_intraday', 'kospi', '2026-02-19T10:00:00+09:00', '2026-02-13T15:30:00+09:00', '2026-02-13', 'open'),
    ('kr_before_boundary', 'kospi', '2026-02-19T16:44:59+09:00', '2026-02-19T15:30:00+09:00', '2026-02-13', None),
    ('kr_at_boundary', 'kospi', '2026-02-19T16:45:00+09:00', '2026-02-19T15:30:00+09:00', '2026-02-19', 'after_close'),
    ('us_mlk', 'sp500', '2026-01-19T12:00:00-05:00', '2026-01-16T16:00:00-05:00', '2026-01-16', 'closed'),
    ('us_before_reopen', 'nasdaq100', '2026-01-20T08:00:00-05:00', '2026-01-16T16:00:00-05:00', '2026-01-16', 'pre_open'),
    ('us_intraday', 'sp500', '2026-01-20T10:00:00-05:00', '2026-01-16T16:00:00-05:00', '2026-01-16', 'open'),
    ('us_before_boundary', 'sp500', '2026-01-20T16:44:59-05:00', '2026-01-20T16:00:00-05:00', '2026-01-16', None),
    ('us_at_boundary', 'sp500', '2026-01-20T16:45:00-05:00', '2026-01-20T16:00:00-05:00', '2026-01-20', 'after_close'),
]


def contract_cases() -> list[dict]:
    cases = []
    for name, key, at, observed, completed, phase in CASES:
        now = datetime.fromisoformat(at)
        market = 'KR' if key in policy.KR_SESSION_KEYS else 'US'
        assert last_completed_session_date(now, market).isoformat() == completed
        source = snapshot(generated_at=now.isoformat())
        source['signals'].update(wti={'value': 75, 'changePct': 0.2, 'status': 'ok'},
                                 gold={'value': 2500, 'changePct': 0.1, 'status': 'ok'})
        source.update(mode='free_cache_experiment', status='partial', paidProviderEnabled=False, clientDirectProviderCalls=False)
        source['news'] = {'generatedAt': at, 'status': 'ok', 'paidProviderEnabled': False,
                          'clientDirectProviderCalls': False, 'bodyScrapingEnabled': False, 'imageScrapingEnabled': False,
                          'items': [{'headline': f'미국 증시 지수 흐름 확인 {index}', 'url': f'https://example.test/news/{index}',
                                     'publishedAt': at, 'sourceName': 'Contract fixture'} for index in range(3)]}
        for signal_key, value in source['signals'].items():
            value.update(key=signal_key, provider='public-chart-delayed', sourceId=f'contract:{signal_key}', valuePolicy='show', dataAsOf=at, fetchedAt=at)
        quote = source['signals'][key]
        quote.update(status='stale', freshnessStatus='stale', dataAsOf=observed, changePct=-1.0)
        reference = policy.session_close_reference(key, quote, now)
        assert (reference is not None) == (phase is not None), name
        if reference:
            assert reference['phase'] == phase and reference['completedSessionDate'] == completed
        point = snapshot_score_point(quote, key, as_of=now)
        assert point['close'] == quote['value'], name
        public = sanitize_public_snapshot(source)
        assert public['signals'][key]['maxStaleAgeHours'] == policy.MAX_STALE_SIGNAL_AGE_HOURS[key]
        assert_public_payload_safe(public)
        control = deepcopy(source)
        control['signals'][key].update(status='ok', dataAsOf=at)
        expected = public_temperature_score_core(control)
        actual = public_temperature_score_core(source)
        assert actual['kr']['score'] == expected['kr']['score'], name
        assert actual['us']['score'] == expected['us']['score'], name
        assert actual['inputPolicyVersion'] == 'last-close-session-v1'
        cases.append({'name': name, 'snapshot': public, 'expectedKr': actual['kr']['score'],
                      'expectedUs': actual['us']['score'], 'key': key, 'phase': phase})
    return cases


def check_session_close_reference() -> None:
    contract_cases()
    now = datetime.fromisoformat('2026-02-18T12:00:00+09:00')
    quote = {'key': 'kospi', 'status': 'stale', 'valuePolicy': 'show', 'value': 2750,
             'changePct': -1, 'dataAsOf': '20260213', 'maxStaleAgeHours': 999999}
    old = {**quote, 'dataAsOf': '20260212', 'sessionCloseReference': {'label': 'UNTRUSTED'}}
    assert snapshot_score_point(old, 'kospi', as_of=now)['close'] is None
    assert snapshot_score_point({**old, 'status': 'suspect'}, 'kospi', as_of=now)['close'] is None
    public = sanitize_public_signal(old, as_of=now)
    assert 'sessionCloseReference' not in public and public['maxStaleAgeHours'] == 96
    assert 'UNTRUSTED' not in json.dumps(public)
    holiday = sanitize_public_snapshot({'generatedAt': now.isoformat(), 'signals': {'kospi': old},
                                       'marketSessions': {'KR': {'phase': 'open'}}})
    assert holiday['marketSessions']['KR']['phase'] == 'closed'
    assert 'sessionCloseReference' not in holiday['signals']['kospi']
    for key in ('usd_krw', 'wti', 'gold', 'us10y', 'dxy'):
        assert policy.session_close_reference(key, quote, now) is None
        assert snapshot_score_point({**quote, 'key': key}, key, as_of=now)['close'] is None
    for changes in ({'value': 0}, {'value': [2750]}, {'changePct': None}, {'valuePolicy': 'hide'},
                    {'dataAsOf': None}, {'dataAsOf': '99991231'}):
        assert policy.session_close_reference('kospi', {**quote, **changes}, now) is None
    us_now = datetime.fromisoformat('2026-01-20T08:00:00-05:00')
    us_quote = {**quote, 'key': 'sp500', 'dataAsOf': '20260116'}
    assert policy.session_close_reference('sp500', us_quote, us_now) is None
    explicit = policy.session_close_reference('sp500', {**us_quote, 'observationDateBasis': 'exchange_date'}, us_now)
    assert explicit['observationDate'] == '2026-01-16'
    assert policy.session_close_reference('kospi', quote, datetime.fromisoformat('2027-02-18T12:00:00+09:00')) is None

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    with patch.object(cache, 'datetime', Frozen), patch.object(policy, 'datetime', Frozen):
        retained = cache.choose_signal({'key': 'kospi', 'label': 'KOSPI'}, {}, {'kospi': quote})
        assert retained['value'] == 2750 and retained['status'] == 'stale'
        assert retained['dataAsOf'] == '20260213'
        missing = cache.choose_signal({'key': 'kospi', 'label': 'KOSPI'}, {}, {'kospi': old})
        assert missing['value'] is None


if __name__ == '__main__':
    check_session_close_reference()
    if '--fixtures-json' in sys.argv:
        print(json.dumps(contract_cases(), ensure_ascii=False))
    else:
        print('PASS KR/US last-session retention, holidays, reopen boundaries and safe metadata')
