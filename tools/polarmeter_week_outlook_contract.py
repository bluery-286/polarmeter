#!/usr/bin/env python3
"""Contract for the week outlook (꽁치의 7일 예보): slots, bar dates, no look-ahead, carry-forward, public shape."""
from __future__ import annotations

import math
import random
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

import polarmeter_week_outlook as wo
from polarmeter_free_cache_worker import sanitize_public_snapshot
from polarmeter_security_contract import assert_public_payload_safe

KST = wo.KST
STAMP_HOUR = {'kr': 0, 'us': 14, 'fut': 5}   # UTC hour Yahoo stamps a winter daily bar at


def kst(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=KST)


def daily_payload(kind: str, last: date, seed: int, level: float = 100.0) -> dict:
    """Two years of made-up daily bars on the market's own open days, ending on `last`."""
    rng = random.Random(seed)
    market = 'kr' if kind == 'kr' else 'us'
    stamps, o, h, l, c = [], [], [], [], []
    day = last - timedelta(days=730)
    while day <= last:
        supported = market == 'us' or day.year in wo.cal.KR_CALENDAR_YEARS
        if (wo.closed_reason(market, day) is None) if supported else day.weekday() < 5:
            open_ = level * (1 + rng.uniform(-0.01, 0.01))
            close = open_ * (1 + rng.uniform(-0.02, 0.02))
            stamps.append(int(datetime.combine(day, time(STAMP_HOUR[kind]), timezone.utc).timestamp()))
            o.append(open_); c.append(close); h.append(max(open_, close) * 1.004); l.append(min(open_, close) * 0.996)
            level = close
        day += timedelta(days=1)
    return {'chart': {'result': [{'timestamp': stamps, 'indicators': {'quote': [{'open': o, 'high': h, 'low': l, 'close': c}]}}]}}


def hourly_payload(end: datetime, drift: float = 0.0004) -> dict:
    stamps, closes, level = [], [], 5000.0
    moment = end - timedelta(days=20)
    while moment <= end:
        level *= 1 + drift
        stamps.append(int(moment.timestamp())); closes.append(level)
        moment += timedelta(hours=1)
    return {'chart': {'result': [{'timestamp': stamps, 'indicators': {'quote': [{'close': closes}]}}]}}


class FakeYahoo:
    def __init__(self, kr_last: date, us_last: date, now: datetime):
        self.kr_last, self.us_last, self.now, self.calls = kr_last, us_last, now, 0

    def __call__(self, url: str):
        self.calls += 1
        if 'interval=1h' in url:
            return hourly_payload(self.now)
        for seed, (key, (symbol, kind)) in enumerate(wo.SYMBOLS.items()):
            if '/' + symbol.replace('^', '%5E').replace('=', '%3D') + '?' in url:
                return daily_payload(kind, self.kr_last if kind == 'kr' else self.us_last, seed, 20.0 if key == 'vix' else 100.0)
        raise AssertionError('unexpected url ' + url)


def boom(url: str):
    raise AssertionError('the network must not be used while the slot is unchanged')


def check_slots() -> None:
    assert wo.kr_slot(kst(2026, 11, 26, 23, 0)) == (date(2026, 11, 27), '2110')
    assert wo.kr_slot(kst(2026, 11, 27, 0, 30)) == (date(2026, 11, 27), '0010')
    assert wo.kr_slot(kst(2026, 11, 27, 6, 55)) == (date(2026, 11, 27), '0650')
    assert wo.kr_slot(kst(2026, 11, 27, 9, 10)) == (date(2026, 11, 27), '0905')
    assert wo.kr_slot(kst(2026, 11, 27, 16, 0)) == (date(2026, 11, 30), 'close')
    assert wo.kr_slot(kst(2026, 11, 27, 21, 15)) == (date(2026, 11, 30), '2110')
    assert wo.kr_slot(kst(2026, 11, 28, 0, 30)) == (date(2026, 11, 30), '0010'), 'Friday night: the US session is still under way'
    assert wo.kr_slot(kst(2026, 11, 28, 10, 0)) == (date(2026, 11, 30), 'after_us'), 'reissued once Friday\'s US session has closed'
    assert wo.kr_slot(kst(2026, 11, 29, 23, 0)) == (date(2026, 11, 30), 'after_us')
    assert wo.kr_slot(kst(2026, 11, 30, 0, 30)) == (date(2026, 11, 30), 'after_us'), 'no US session ran on Sunday night'
    assert wo.kr_slot(kst(2026, 12, 1, 0, 30)) == (date(2026, 12, 1), '0010')
    # US: Thanksgiving (26th) is closed, so the session after the 25th is the 27th, called from 21:10 KST that day
    assert wo.us_slot(kst(2026, 11, 26, 12, 0)) == (date(2026, 11, 27), 'close')
    assert wo.us_slot(kst(2026, 11, 27, 21, 15)) == (date(2026, 11, 27), '2110')
    assert wo.us_slot(kst(2026, 11, 28, 4, 0)) == (date(2026, 11, 30), 'close'), 'the 27th closes early at 13:00 New York'


def check_bar_dates() -> None:
    """A futures bar stamped 04:00 UTC in summer belongs to that New York day, not the day before."""
    summer = int(datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc).timestamp())
    payload = {'chart': {'result': [{'timestamp': [summer], 'indicators': {'quote': [{'open': [1.0], 'high': [1.0], 'low': [1.0], 'close': [1.0]}]}}]}}
    assert list(wo.parse_daily(payload, 'fut')) == [date(2026, 7, 15)]
    assert 'krw' not in wo.SYMBOLS and all(kind in wo.KINDS for _, kind in wo.SYMBOLS.values()), 'FX day bars end at 09:00 KST'


def check_no_lookahead() -> None:
    now = kst(2026, 11, 27, 6, 55)
    book, _ = wo.fetch_book(FakeYahoo(date(2026, 11, 26), date(2026, 11, 25), now))
    day = date(2026, 11, 10)
    before = (wo.kr_direction_features(book, day), wo.swing_features(book, 'kr', day))
    changed = {key: {d: (bar if d < day else tuple(v * 3 for v in bar)) for d, bar in rows.items()} for key, rows in book.bars.items()}
    after_book = wo.Book(changed)
    assert before == (wo.kr_direction_features(after_book, day), wo.swing_features(after_book, 'kr', day)), \
        'a forecast for a day must not move when that day or later days change'
    assert before[0] is not None and set(before[0]) == set(wo.KR_DIRECTION_FEATURES)


def check_morning_issue() -> dict:
    now = kst(2026, 11, 27, 6, 55)
    yahoo = FakeYahoo(date(2026, 11, 26), date(2026, 11, 25), now)
    snapshot = wo.apply_week_outlook({}, None, now, yahoo)
    block = snapshot['weekOutlook']
    assert block['status'] == 'ready', block
    wo.assert_public_week_outlook(block)
    kr, us = block['markets']['kr'], block['markets']['us']
    assert (kr['target'], kr['slot'], kr['basisSession']) == ('2026-11-27', '0650', '2026-11-26')
    # 11-26 was Thanksgiving: the last US close (11-25) was already in the Korean 11-26 session, so 06:50 says nothing.
    assert kr['open']['call'] == 'none' and kr['close']['call'] == 'none', 'no call from a two-day-old US session'
    assert kr['open']['basis'] == kr['close']['basis'] == 'us_closed'
    assert [row['closed'] for row in kr['days']] == [False, True, True, False, False, False, False]
    assert kr['days'][1]['closedReason'] and [row['sessionsAhead'] for row in kr['days'] if not row['closed']] == [0, 1, 2, 3, 4]
    assert (us['target'], us['slot']) == ('2026-11-27', 'close') and us['close'] is None, 'no US call before 21:10'
    assert_public_payload_safe({'weekOutlook': block})
    published = sanitize_public_snapshot({'generatedAt': '2026-11-26T21:55:00Z', 'weekOutlook': block})
    assert published['weekOutlook'] == block, 'sanitize_public_snapshot must pass weekOutlook through unchanged'
    return block


def check_morning_after_us_session() -> None:
    # A normal morning (the US traded the night before) still gives the 06:50 call from the US session.
    now = kst(2026, 11, 26, 6, 55)
    block = wo.apply_week_outlook({}, None, now, FakeYahoo(date(2026, 11, 25), date(2026, 11, 25), now))['weekOutlook']
    wo.assert_public_week_outlook(block)
    kr = block['markets']['kr']
    assert (kr['target'], kr['slot'], kr['basisSession']) == ('2026-11-26', '0650', '2026-11-25')
    assert kr['open']['basis'] == 'us_session' and kr['close']['basis'] == 'us_session'


def check_carry_and_reissue(block: dict) -> None:
    # same slot: nothing is fetched and the issued entries stay as they were
    later = wo.apply_week_outlook({}, {'weekOutlook': block}, kst(2026, 11, 27, 8, 30), boom)['weekOutlook']
    assert later['markets'] == block['markets'] and later['status'] == 'ready'
    # 09:05: the close call is redone from the actual open; the US entry is still carried
    now = kst(2026, 11, 27, 9, 10)
    again = wo.apply_week_outlook({}, {'weekOutlook': block}, now, FakeYahoo(date(2026, 11, 27), date(2026, 11, 25), now))['weekOutlook']
    kr = again['markets']['kr']
    assert kr['slot'] == '0905' and kr['close']['basis'] == 'actual_open' and 'actualPct' in kr['open']
    assert again['markets']['us'] == block['markets']['us']
    # the open is not in yet: the 06:50 entry stays and the block says what it waits for
    waiting = wo.apply_week_outlook({}, {'weekOutlook': block}, now, FakeYahoo(date(2026, 11, 26), date(2026, 11, 25), now))['weekOutlook']
    assert waiting['status'] == 'partial' and waiting['waiting'] == {'kr': 'kr_open_missing'} and waiting['markets']['kr'] == block['markets']['kr']
    # 21:10: both markets get a futures call (or say none), measured from their own last close
    now = kst(2026, 11, 27, 21, 15)
    night = wo.apply_week_outlook({}, {'weekOutlook': again}, now, FakeYahoo(date(2026, 11, 27), date(2026, 11, 25), now))['weekOutlook']
    wo.assert_public_week_outlook(night)
    assert night['markets']['kr']['target'] == '2026-11-30' and night['markets']['kr']['close']['basis'] == 'sp_futures_since_kr_close'
    assert night['markets']['us']['close']['basis'] == 'sp_futures_since_us_close' and night['markets']['us']['close']['futuresPct'] > 0


def check_never_raises() -> None:
    now = kst(2026, 11, 27, 6, 55)
    def down(url: str):
        raise OSError('network down')
    block = wo.apply_week_outlook({}, None, now, down)['weekOutlook']
    assert block['status'] == 'unavailable' and block['markets'] == {} and block['reason'] == 'OSError'
    wo.assert_public_week_outlook(block)
    old = check_morning_issue()
    stale = wo.apply_week_outlook({}, {'weekOutlook': old}, kst(2026, 11, 27, 9, 10), down)['weekOutlook']
    assert stale['status'] == 'stale' and stale['markets'] == old['markets'], 'a failed run keeps the last issued forecast'


def check_tiers() -> None:
    model = wo.load_model()
    assert model['modelVersion'] == wo.MODEL_VERSION
    for part in (model['kr']['close'], model['kr']['open'], model['kr']['closeAfterOpen'], model['us']['futures']['2110']):
        assert part['clearAbove'] > part['leanAbove'] > 0 and part['hitPct']['clear'] > part['hitPct']['lean'] > part['alwaysUpPct']
    assert set(model['kr']['futures']) == {'2110', '0010', 'after_us'}
    weak = model['kr']['futures']['2110']
    said = wo.call_from(0.5, (weak['leanAbove'] + weak['clearAbove']) / 2, weak, 'x')
    assert weak['hitPct']['lean'] < weak['alwaysUpPct'] + wo.TIER_EDGE_PCT and said['call'] == 'none' and said['tier'] == 'none', \
        'a tier that does not beat always-up is not spoken'
    for market in ('kr', 'us'):
        swing = model[market]['swing']
        assert len(swing['horizons']) == wo.HORIZONS
        for horizon in swing['horizons']:
            cuts, record = horizon['stageCuts'], horizon['stageRecord']
            assert cuts == sorted(cuts) and record[3]['meanAbsPct'] > record[0]['meanAbsPct'], 'a rougher stage moved more'
    record = model['record']
    assert all(76 <= v <= 84 for v in record['krSwing']['rangeInsidePct'] + record['usSwing']['rangeInsidePct']), '8 in 10 means 8 in 10'


def check_track() -> None:
    """The live ledger: an issue is written once per slot, a changed direction is logged as a revision, and a
    session is scored only after it has settled — from the real bar, never from a later forecast."""
    now = kst(2026, 11, 27, 6, 55)
    yahoo = FakeYahoo(date(2026, 11, 26), date(2026, 11, 25), now)
    first = wo.apply_week_outlook({}, None, now, yahoo)['weekOutlook']
    track = first['track']
    assert track['since'] == '2026-11-27' and track['revisions'] == []
    kr = next(r for r in track['rows'] if r['market'] == 'kr')
    assert kr['target'] == '2026-11-27' and [i['slot'] for i in kr['issues']] == ['0650'] and kr['actual'] is None
    assert first['regime']['kr']['state'] in {'rougher', 'same', 'calmer'} and first['regime']['kr']['through'] == '2026-11-26'

    # Same slot again: nothing fetched, nothing added.
    same = wo.apply_week_outlook({}, {'weekOutlook': first}, kst(2026, 11, 27, 8, 0), boom)['weekOutlook']
    assert same['track']['rows'] == track['rows'] and same['regime'] == first['regime']

    # A direction said at 06:50 and said differently at 09:05 becomes a revision; an unchanged one does not.
    forced = deepcopy(first)
    said = next(r for r in forced['track']['rows'] if r['market'] == 'kr')['issues'][0]
    said['close'] = 'down:clear'
    now2 = kst(2026, 11, 27, 9, 6)
    second = wo.apply_week_outlook({}, {'weekOutlook': forced}, now2, FakeYahoo(date(2026, 11, 27), date(2026, 11, 25), now2))['weekOutlook']
    kr2 = next(r for r in second['track']['rows'] if r['market'] == 'kr')
    assert [i['slot'] for i in kr2['issues']] == ['0650', '0905'] and kr2['actual'] is None
    new_close = kr2['issues'][1]['close']
    changes = [c for c in second['track']['revisions'] if c['kind'] == 'close']
    assert (len(changes) == 1 and changes[0]['from'] == 'down:clear' and changes[0]['to'] == new_close) or new_close == 'down:clear'

    # A day whose swing stage was logged differently yesterday shows up as a swing revision.
    forced2 = deepcopy(second)
    monday = next(d for d in forced2['markets']['kr']['days'] if not d['closed'] and d['sessionsAhead'] == 1)
    forced2['track']['dayLog']['kr'][monday['date']] = 1 if monday['swingStage'] != 1 else 2
    now3 = kst(2026, 11, 27, 15, 40)
    third = wo.apply_week_outlook({}, {'weekOutlook': forced2}, now3, FakeYahoo(date(2026, 11, 27), date(2026, 11, 25), now3))['weekOutlook']
    assert third['markets']['kr']['target'] == '2026-11-30' and third['markets']['kr']['slot'] == 'close'
    assert any(c['kind'] == 'swing' and c['date'] == monday['date'] for c in third['track']['revisions'])
    friday = next(r for r in third['track']['rows'] if r['market'] == 'kr' and r['target'] == '2026-11-27')
    assert friday['actual'] is None, 'ten minutes after the bell the bar is not settled yet'

    # Sunday night: Friday has settled, so its issues get their results and the totals count them.
    now4 = kst(2026, 11, 29, 21, 12)
    fourth = wo.apply_week_outlook({}, {'weekOutlook': third}, now4, FakeYahoo(date(2026, 11, 27), date(2026, 11, 27), now4))['weekOutlook']
    friday = next(r for r in fourth['track']['rows'] if r['market'] == 'kr' and r['target'] == '2026-11-27')
    assert friday['actual'] is not None and isinstance(friday['rangeInside'], bool)
    close_pct = friday['actual']['closePct']
    for issue in friday['issues']:
        label = issue['close']
        expected = None if label in (None, 'none') or close_pct == 0 else (label.startswith('up') == (close_pct > 0))
        assert issue['closeHit'] is expected
    assert friday['issues'][1]['openHit'] is None, 'the 09:05 issue already knew the open'
    totals = fourth['track']['totals']['kr']
    assert totals['sessions'] == 1 and totals['rangeDays'] == 1
    assert sum(cell['closeCalls'] for cell in totals['slots'].values()) == sum(1 for i in friday['issues'] if i['closeHit'] is not None)
    wo.assert_public_week_outlook(fourth)

    # An outage keeps the ledger.
    def down(url: str):
        raise OSError('network down')
    after = wo.apply_week_outlook({}, {'weekOutlook': fourth}, kst(2026, 11, 30, 6, 55), down)['weekOutlook']
    assert after['status'] == 'stale', (after['status'], after.get('reason'))
    assert after['track'] == fourth['track']


def main() -> int:
    check_slots()
    check_bar_dates()
    check_no_lookahead()
    block = check_morning_issue()
    check_morning_after_us_session()
    check_carry_and_reissue(deepcopy(block))
    check_never_raises()
    check_tiers()
    check_track()
    print('week outlook contract: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
