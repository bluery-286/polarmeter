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
    assert wo.kr_slot(kst(2026, 11, 28, 10, 0)) == (date(2026, 11, 30), '0010'), 'a weekend keeps the futures view of Monday'
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
    assert kr['open'] is not None and kr['close'] is not None and kr['close']['basis'] == 'us_session'
    assert [row['closed'] for row in kr['days']] == [False, True, True, False, False, False, False]
    assert kr['days'][1]['closedReason'] and [row['sessionsAhead'] for row in kr['days'] if not row['closed']] == [0, 1, 2, 3, 4]
    assert (us['target'], us['slot']) == ('2026-11-27', 'close') and us['close'] is None, 'no US call before 21:10'
    assert_public_payload_safe({'weekOutlook': block})
    published = sanitize_public_snapshot({'generatedAt': '2026-11-26T21:55:00Z', 'weekOutlook': block})
    assert published['weekOutlook'] == block, 'sanitize_public_snapshot must pass weekOutlook through unchanged'
    return block


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


def main() -> int:
    check_slots()
    check_bar_dates()
    check_no_lookahead()
    block = check_morning_issue()
    check_carry_and_reissue(deepcopy(block))
    check_never_raises()
    check_tiers()
    print('week outlook contract: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
