"""Offline checks for daily-close copy without weakening quote safety."""
from copy import deepcopy
import json
from datetime import datetime
from unittest.mock import Mock, patch

import polarmeter_cache_snapshot as cache
import polarmeter_free_provider_probe as probe
from polarmeter_last_close_policy import valid_last_close

from polarmeter_free_cache_worker import (
    assert_public_payload_safe, daily_close_reference, sanitize_public_signal,
    snapshot_score_point,
)


def check_daily_close_reference() -> None:
    now = datetime.fromisoformat('2026-10-19T10:36:30+09:00')
    signal = {
        'key': 'kr_samsung', 'provider': 'data-go-kr-free',
        'value': '210000', 'changePct': '-9.48', 'dataAsOf': '20261015',
        'dataAgeHours': 97.61, 'status': 'stale', 'valuePolicy': 'show',
        'dailyCloseReference': {'label': 'untrusted provider copy'},
        'qualityReason': 'private diagnostic', 'fallbackChain': ['private diagnostic'],
    }
    before = deepcopy(signal)
    public = sanitize_public_signal(signal, as_of=now)
    reference = public['dailyCloseReference']
    assert reference['label'] == '종가 참고자료 · 실시간 아님'
    assert reference['observedDate'] == '2026-10-15'
    assert reference['expectedUpdateAt'] == '2026-10-19T04:00:00Z'
    assert reference['scheduleStatus'] == 'awaiting_window'
    for text in ('종가 참고자료', '기준일 2026-10-15', '10/19 오후 1시 이후 예상'):
        assert text in public['publicReasonLabel']
    assert public['dataAsOf'] == signal['dataAsOf']
    assert public['status'] == 'stale'
    assert snapshot_score_point(public, 'kr_samsung')['close'] == signal['value']
    assert snapshot_score_point(public, 'kr_samsung')['pct'] == signal['changePct']
    assert reference['calculationLabel'] == '마지막 확인 종가로 계산'
    assert signal == before, 'disclosure must not mutate source or scoring inputs'
    assert 'untrusted' not in str(public) and 'private diagnostic' not in str(public)
    assert_public_payload_safe({'signals': {'kr_samsung': public}})

    # A window is an expectation, never a promise that new quotes arrived.
    after = datetime.fromisoformat('2026-10-19T14:00:00+09:00')
    late = daily_close_reference(signal, after)
    assert late['expectedUpdateAt'] == reference['expectedUpdateAt']
    assert late['scheduleStatus'] == 'check_provider_update'
    assert '새 자료 확인' in late['updateLabel'] and '예상' in late['updateLabel']
    assert daily_close_reference(signal, datetime.fromisoformat('2026-10-19T13:00:00+09:00'))['scheduleStatus'] == 'check_provider_update'
    for when in ('2026-10-17T12:00:00+09:00', '2026-10-18T12:00:00+09:00'):
        assert daily_close_reference(signal, datetime.fromisoformat(when))['expectedUpdateAt'] == reference['expectedUpdateAt']
    holiday = daily_close_reference({**signal, 'dataAsOf': '20261007'}, now)
    assert holiday['expectedUpdateAt'] == '2026-10-12T04:00:00Z'
    for observed in ('20261230', '20270104'):
        no_calendar = daily_close_reference({**signal, 'dataAsOf': observed}, datetime.fromisoformat('2027-01-05T12:00:00+09:00'))
        assert no_calendar['expectedUpdateAt'] is None
        assert no_calendar['scheduleStatus'] == 'calendar_unavailable'
    for observed in (None, '', '20260230', '20261020', True, 'provider://untrusted'):
        invalid = daily_close_reference({**signal, 'dataAsOf': observed}, now)
        assert invalid['observedDate'] is None and invalid['expectedUpdateAt'] is None
        assert invalid['scheduleStatus'] == 'date_unavailable'
    for changes in ({'key': 'vix'}, {'provider': 'other'}, {'status': 'invalid'}, {'valuePolicy': 'hide'}):
        assert daily_close_reference({**signal, **changes}, now) is None
    for status in ('ok', 'stale', 'suspect'):
        item = {**signal, 'status': status}
        exposed = sanitize_public_signal(item, as_of=now)
        assert exposed['status'] == status
        assert '실시간 아님' in exposed['publicReasonLabel']
        if status == 'suspect':
            assert exposed['publicReasonCode'] == 'low_confidence_review'
        assert snapshot_score_point(exposed, 'kr_samsung') == snapshot_score_point(item, 'kr_samsung')

    # Age is a disclosure/confidence signal, never a reason to delete a valid
    # last observed close. Invalid observations remain unscoreable.
    for age in (48, 97.61, 41 * 24):
        point = snapshot_score_point({**signal, 'dataAgeHours': age}, 'kr_samsung')
        assert point['close'] == signal['value'] and point['pct'] == signal['changePct']
    for changes in ({'value': 0}, {'value': -1}, {'value': True}, {'value': float('inf')},
                    {'value': [210000]}, {'changePct': [-9.48]}, {'value': '0x100'}, {'value': '210_000'},
                    {'changePct': None}, {'changePct': float('nan')}, {'changePct': -100},
                    {'dataAsOf': None}, {'dataAsOf': '20260230'}, {'dataAsOf': '99991231'},
                    {'dataAsOf': '9999-01-01T00:00:00Z'}, {'status': 'invalid'}):
        invalid = {**signal, **changes}
        assert not valid_last_close(invalid)
        assert snapshot_score_point(invalid, 'kr_samsung')['close'] is None

    last = {**signal, 'dataAsOf': '20261016', 'value': '220000'}
    old_item = {'key': 'kr_samsung', 'status': 'ok', 'price': '210000',
                'changePct': '-9.48', 'asOf': '20261014'}
    selected = cache.choose_signal({'key': 'kr_samsung', 'label': '삼성전자'},
                                   {'data-go-kr-free': [old_item]}, {'kr_samsung': last})
    assert selected['dataAsOf'] == last['dataAsOf'] and selected['value'] == last['value']
    assert selected['status'] == 'stale'
    retained = cache.choose_signal({'key': 'kr_samsung'}, {}, {'kr_samsung': signal})
    assert retained['value'] == signal['value'] and retained['dataAsOf'] == signal['dataAsOf']
    # An invalid new observation cannot replace the valid retained close.
    for changes in ({'asOf': None}, {'asOf': '99991231'}, {'price': float('nan')}):
        selected = cache.choose_signal({'key': 'kr_samsung'}, {'data-go-kr-free': [{**old_item, **changes}]}, {'kr_samsung': signal})
        assert selected['dataAsOf'] == signal['dataAsOf'] and selected['value'] == signal['value']

    fake_path = Mock()
    fake_path.exists.return_value = True
    fake_path.read_text.return_value = json.dumps({'signals': {'kr_samsung': last}})
    with patch.object(cache, 'atomic_write_json') as write:
        cache.write_last_known_good(fake_path, {'signals': {'kr_samsung': signal}})
    stored = write.call_args.args[1]['signals']['kr_samsung']
    assert stored['dataAsOf'] == last['dataAsOf'], 'older provider rows must not regress the retained close'
    fake_path.exists.return_value = False
    with patch.object(cache, 'atomic_write_json') as write:
        cache.write_last_known_good(fake_path, {'signals': {'kr_samsung': signal}})
    assert write.call_args.args[1]['signals']['kr_samsung']['dataAsOf'] == signal['dataAsOf']

    rows = [
        {'srtnCd': '005930', 'basDt': '20261015', 'clpr': '210000', 'fltRt': '-9.48'},
        {'srtnCd': '005935', 'basDt': '20261016', 'clpr': '999999', 'fltRt': '1'},
        {'srtnCd': '005930', 'basDt': '20261016', 'clpr': '220000', 'fltRt': '4.76'},
        {'srtnCd': '005930', 'basDt': '99991231', 'clpr': '999999', 'fltRt': '1'},
    ]
    response = {'response': {'body': {'items': {'item': rows}}}}
    with patch.object(probe, 'fetch_json', return_value=response):
        item = probe.data_go_kr_probe('offline-test-key')['items'][0]
    assert item['asOf'] == '20261016' and item['price'] == '220000'


if __name__ == '__main__':
    check_daily_close_reference()
    print('PASS retained last-close calculation, newest-date selection, validity and daily-close disclosure')
