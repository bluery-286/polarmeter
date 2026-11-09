#!/usr/bin/env python3
"""Contract for the evening brief (꽁치의 저녁 브리핑): calendar, inputs, freeze, scoring, copy and public shape."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

import polarmeter_cache_snapshot as snap
import polarmeter_evening_brief as eb

NY, KST = eb.NY, eb.KST


def bars_for(days: dict[str, tuple[float, float]], first: date, last: date) -> list[tuple[int, float]]:
    """Hourly bars: each session closes at `close` at 16:00 New York and trades at `evening` from 21:00 KST
    the next session day (the price it shows that evening)."""
    out = []
    moment = datetime.combine(first, time(0), timezone.utc)
    end = datetime.combine(last + timedelta(days=1), time(23), timezone.utc)
    keys = sorted(days)
    while moment <= end:
        ny_day = (moment + timedelta(hours=1)).astimezone(NY).date().isoformat()  # the hour the bar ends in
        past = [k for k in keys if eb.close_at(date.fromisoformat(k)) <= moment + timedelta(hours=1)]
        price = 100.0
        if past:
            price = days[past[-1]][0]
        if ny_day in days and moment + timedelta(hours=1) >= eb.issue_from(date.fromisoformat(ny_day)) - timedelta(hours=9) \
                and past and past[-1] < ny_day:
            price = days[past[-1]][0] * (1 + days[ny_day][1] / 100)
        out.append((int(moment.timestamp()), price))
        moment += timedelta(hours=1)
    return out


def sample_data(start_pct: float = 0.6, close_pct: float = 0.9) -> tuple[dict, date]:
    """Sessions 2026-10-08..2026-11-10 with a flat history; the last one starts +start_pct and closes +close_pct."""
    target = date(2026, 11, 10)
    sessions = list(reversed(eb.us_days_back(target, 25)))
    closes, index = {}, {}
    level = 100.0
    for day in sessions:
        key = day.isoformat()
        move = start_pct if day == target else 0.1
        closes[key] = (level, move)
        index[key] = (level, level)
    prev = eb.us_day_before(target).isoformat()
    index[target.isoformat()] = (index[prev][1] * 1.004, index[prev][1] * (1 + close_pct / 100))
    bars = bars_for(closes, sessions[0] - timedelta(days=2), target)
    return {'futures': {'esPct': bars, 'nqPct': bars}, 'index': index}, target


MODEL = eb.load_model()


def check_calendar() -> None:
    assert eb.rest_reason(date(2026, 10, 28)) == 'fomc'
    assert eb.rest_reason(date(2026, 12, 9)) == 'fomc'
    assert eb.third_friday(2026, 12) == date(2026, 12, 18)
    assert eb.rest_reason(date(2026, 12, 7)) == 'futures_roll' and eb.rest_reason(date(2026, 12, 21)) is None
    assert eb.rest_reason(date(2026, 11, 10)) is None
    # The snapshot's FOMC schedule and this module's list agree on every scheduled decision day.
    scheduled = {datetime.fromisoformat(at.replace('Z', '+00:00')).astimezone(NY).date()
                 for at, _ in snap.SCHEDULED_MACRO_EVENTS['fomc_rate']}
    assert scheduled <= eb.FOMC_DECISION_DATES, sorted(scheduled - eb.FOMC_DECISION_DATES)
    # Target: today's US session until 16:15 New York, then the next session (Friday -> Monday).
    assert eb.target_session_for(datetime(2026, 11, 10, 12, 5, tzinfo=timezone.utc)) == date(2026, 11, 10)
    assert eb.target_session_for(datetime(2026, 11, 13, 21, 20, tzinfo=timezone.utc)) == date(2026, 11, 16)


def check_inputs() -> None:
    data, target = sample_data()
    at = datetime.combine(target, time(12, 5), timezone.utc)
    pct = eb.futures_move(data['futures']['esPct'], eb.us_day_before(target).isoformat(), at)
    assert pct is not None and abs(pct - 0.6) < 1e-9, pct
    assert eb.futures_move([], '2026-11-09', at) is None
    stale = [(t, c) for t, c in data['futures']['esPct'] if t + 3600 <= at.timestamp() - 5 * 3600]
    assert eb.futures_move(stale, eb.us_day_before(target).isoformat(), at) is None, 'a stale price is not a start'


def check_issue_freeze_and_score() -> None:
    data, target = sample_data(0.6, 0.9)
    before = datetime.combine(target, time(20, 30), KST)
    block = eb.compute_block(None, data, before, MODEL, [])
    eb.assert_public_evening_brief(block)
    assert block['ledger'] and all(r['origin'] == 'backfill' for r in block['ledger']), 'first run: backfill only'
    assert target.isoformat() not in {r['targetSession'] for r in block['ledger']}, 'nothing for tonight before 21:00'
    assert block['current'] is None and block['phase'] == 'off'

    issue = datetime.combine(target, time(21, 15), KST)
    events = [{'id': 'us_cpi', 'at': '2026-11-10T13:30:00Z', 'labelKo': '10월 CPI 발표', 'labelEn': 'US CPI'}]
    block = eb.compute_block({'eveningBrief': block}, data, issue, MODEL, events)
    current = block['current']
    assert current['state'] == 'issued' and current['call'] == 'up' and current['origin'] == 'live'
    assert current['inputs']['esPct'] == 0.6 and current['stats']['band'] == '0.5-0.8'
    assert block['phase'] == 'evening'
    copy = current['copy']
    assert copy['title']['ko'] == '오늘 밤 미국장, 강세 출발 구간이에요'
    assert '플러스를 지켰어요' in copy['hold']['ko'] and '반반' in copy['caveat']['ko']
    assert copy['events'] and copy['events'][0]['kst'] == '22:30'

    # Frozen: a later run with a different futures price does not touch tonight's row.
    moved = deepcopy(data)
    moved['futures']['esPct'] = [(t, c * 0.98) for t, c in moved['futures']['esPct']]
    later = datetime.combine(target, time(23, 45), KST)  # 09:45 New York in November
    block2 = eb.compute_block({'eveningBrief': block}, moved, later, MODEL, events)
    assert block2['current']['inputs'] == current['inputs'] and block2['current']['issuedAt'] == current['issuedAt']
    assert block2['phase'] == 'us_open'

    # Scored after 16:15 New York.
    after = eb.close_at(target) + timedelta(minutes=20)
    block3 = eb.compute_block({'eveningBrief': block2}, data, after, MODEL, events)
    current3 = block3['current']
    assert current3['state'] == 'scored' and current3['outcome'] == 'held', current3
    assert current3['actual']['closePct'] == 0.9 and block3['phase'] == 'scored'
    assert 'S&P 500 +0.90% 마감' in current3['copy']['result']['ko']
    assert block3['scorecard']['liveTotal'] == {'calls': 1, 'held': 1, 'flat': 0, 'flipped': 0}
    assert block3['nextIssueAt'] == eb.iso_utc(eb.issue_from(date(2026, 11, 11)))


def check_outcomes() -> None:
    assert eb.outcome_of('up', 0.15) == 'flat' and eb.outcome_of('down', -0.19) == 'flat'
    assert eb.outcome_of('up', 0.5) == 'held' and eb.outcome_of('up', -0.5) == 'flipped'
    assert eb.outcome_of('down', -0.5) == 'held'
    data, target = sample_data(0.1, 0.5)
    issue = datetime.combine(target, time(21, 15), KST)
    first = eb.compute_block(None, data, datetime.combine(target, time(20, 0), KST), MODEL, [])
    block = eb.compute_block({'eveningBrief': first}, data, issue, MODEL, [])
    assert block['current']['call'] == 'none' and block['current']['stats'] is None
    assert '뚜렷하지 않아요' in block['current']['copy']['title']['ko'] and 'hold' not in block['current']['copy']
    after = eb.close_at(target) + timedelta(minutes=20)
    scored = eb.compute_block({'eveningBrief': block}, data, after, MODEL, [])
    assert scored['current']['state'] == 'scored' and scored['current']['outcome'] is None
    assert scored['scorecard']['liveTotal']['calls'] == 0, 'a no-direction night is not a call'


def check_rest_and_missed() -> None:
    data, _ = sample_data()
    fomc = date(2026, 10, 28)
    row = eb.decide(eb.new_row(fomc.isoformat(), '2026-10-27', 'live'), data, datetime.combine(fomc, time(12, 5), timezone.utc),
                    MODEL, None)
    assert row['state'] == 'rest' and row['restReason'] == 'fomc' and row['call'] is None
    assert '쉬어요' in eb.build_copy(row, [])['title']['ko']
    data, target = sample_data()
    first = eb.compute_block(None, data, datetime.combine(target, time(20, 0), KST), MODEL, [])
    late = eb.compute_block({'eveningBrief': first}, data, eb.open_at(target) + timedelta(minutes=5), MODEL, [])
    assert late['current']['state'] == 'rest' and late['current']['restReason'] == 'missed_window'
    empty = {'futures': {'esPct': [], 'nqPct': []}, 'index': data['index']}
    rows = eb.compute_block({'eveningBrief': first}, empty, datetime.combine(target, time(21, 15), KST), MODEL, [])
    assert rows['current']['restReason'] == 'data_unavailable'


def check_unavailable_keeps_ledger() -> None:
    data, target = sample_data()
    block = eb.compute_block(None, data, datetime.combine(target, time(20, 0), KST), MODEL, [])
    kept = eb.unavailable_block({'eveningBrief': block}, datetime.combine(target, time(21, 0), KST), RuntimeError('x'))
    assert kept['status'] == 'unavailable' and kept['ledger'] == block['ledger'] and kept['phase'] == 'off'
    eb.assert_public_evening_brief(kept)
    snapshot: dict = {}
    eb.apply_evening_brief(snapshot, None, datetime(2026, 11, 10, 12, 5, tzinfo=timezone.utc),
                           fetch=lambda url: (_ for _ in ()).throw(OSError('offline')))
    assert snapshot['eveningBrief']['status'] == 'unavailable', 'a network failure never raises'


def check_model() -> None:
    assert MODEL['modelVersion'] == eb.MODEL_VERSION
    assert set(MODEL['bands']) == {'0.3-0.5', '0.5-0.8', '0.8+'}
    for cell in MODEL['bands'].values():
        assert cell['days'] == cell['held'] + cell['flat'] + cell['flipped'] and cell['days'] >= 30
    assert 0.4 < MODEL['afterOpenSameShare'] < 0.65, 'the after-open share is about a coin flip'


def sample_evening_block() -> dict:
    data, target = sample_data()
    first = eb.compute_block(None, data, datetime.combine(target, time(20, 0), KST), MODEL, [])
    return eb.compute_block({'eveningBrief': first}, data, datetime.combine(target, time(21, 15), KST), MODEL, [])


def main() -> int:
    check_calendar()
    check_inputs()
    check_issue_freeze_and_score()
    check_outcomes()
    check_rest_and_missed()
    check_unavailable_keeps_ledger()
    check_model()
    print('PolarMeter evening brief contract: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
