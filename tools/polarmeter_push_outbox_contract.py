#!/usr/bin/env python3
"""Offline contract: the push outbox (보낼 알림 목록), SPEC §13.3-§13.6, §6.5, bundle 1-D.

Fixed in-code data, no network. Covers the send time (08:05 Seoul time all year, on both sides of the US daylight-saving
switch), weekends and KRX holidays, the topic set of a call day and a no-call day, the 08:55 and 18:00 expiry, no result
before the close is scored, paused / unavailable / no-forecast days, one id per day and kind, the copy of the four
SPEC §13.4 examples, the §6.5 copy rules and length limits in both languages, a miss day, the cap, the public shape, and
never raising.

  PYTHONPATH=tools python3 tools/polarmeter_push_outbox_contract.py [--samples-dir DIR]
"""
from __future__ import annotations

import argparse
import inspect
import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polarmeter_morning_forecast as mf
import polarmeter_morning_forecast_contract as mfc
import polarmeter_evening_brief_contract as ebc
import polarmeter_push_outbox as po

KST = ZoneInfo('Asia/Seoul')
ALL_ITEMS: list[tuple[str, dict]] = []   # every item the cases build; the copy-rule pass at the end reads all of them


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=KST)


# ---------------------------------------------------------------- fixtures

def current_row(call: str = 'down', *, target: str = '2026-11-06', state: str = 'issued', open_pct: float | None = None,
                spy: float = -4.41, soxx: float = -12.15, excess: float = -8.18, actual_open: float | None = None,
                actual_close: float | None = None, hit: bool | None = None, origin: str = 'live', paused: bool = False) -> dict:
    """A `morningForecast.current` with the real copy builder's text (the push layer reads `copy`, `actual`, `result`)."""
    size = open_pct if open_pct is not None else {'down': -3.22, 'up': 3.22, 'none': 0.1}[call]
    strength = 'weak' if call == 'none' else 'strong' if abs(size) >= 1.0 else 'moderate'
    row = {'targetSession': target, 'basisKrSession': '2026-11-05', 'usSessions': ['2026-11-05'], 'origin': origin,
           'state': state, 'issuedAt': '2026-11-05T21:45:00Z', 'noForecastReason': None,
           'inputs': {'spyPct': spy, 'soxxPct': soxx, 'ewyPct': -13.46, 'kospiPrevPct': -5.28, 'ewyExcessPct': excess},
           'predicted': {'openPct': size, 'closePct': size * 0.9, 'openBand': [size - 1, size + 1], 'strength': strength,
                         'openCall': call, 'closeCall': call, 'topDriver': 'ewyExcessPct'},
           'analog': None, 'actual': None, 'result': None}
    if actual_open is not None:
        row['actual'] = {'openPct': actual_open, 'closePct': actual_close,
                         'intradayPct': None if actual_close is None else round(actual_close - actual_open, 2),
                         'source': 'naver_index_price'}
        row['result'] = {'openHit': hit, 'closeHit': None if actual_close is None else hit,
                         'openInBand': None if call == 'none' or hit is None else bool(hit)}
    if paused:
        row['paused'] = True
    row['copy'] = mf.build_copy(row, date.fromisoformat(target), mfc.FULL_MODEL)
    return row


def scorecard(calls: int = 14, hits: int = 13, *, live_sessions: int = 20, live_calls: int | None = None,
              live_hits: int | None = None) -> dict:
    """By default all 20 recent sessions are live forecasts (the "최근" wording). `live_sessions` below 20 means part of
    the window was recomputed from past data; `live_calls`/`live_hits` are then the running live totals."""
    return {'recent': {'sessions': 20, 'calls': calls, 'openHits': hits, 'closeCalls': 0, 'closeHits': 0,
                       'liveSessions': live_sessions, 'marks': ['none'] * 20},
            'live': {'since': '2026-11-06', 'calls': calls if live_calls is None else live_calls,
                     'openHits': hits if live_hits is None else live_hits}}


def snapshot(current: dict | None, *, status: str = 'ok', card: dict | None = None) -> dict:
    return {'generatedAt': '2026-11-06T00:00:00Z',
            'morningForecast': {'schemaVersion': mf.SCHEMA_VERSION, 'status': status, 'phase': 'pre_open', 'current': current,
                                'scorecard': card if card is not None else scorecard()}}


def build(snap: dict, now: str, label: str = '') -> list[dict]:
    items = po.build_items(snap, at(now))
    for item in items:
        ALL_ITEMS.append((label or f'{item["id"]} @ {now}', item))
    return items


def only(items: list[dict]) -> dict:
    assert len(items) == 1, items
    return items[0]


def ids(items: list[dict]) -> list[str]:
    return [item['id'] for item in items]


# ---------------------------------------------------------------- time windows

def check_send_time_ignores_daylight_saving() -> None:
    # 08:05 Seoul is 23:05 UTC the day before, summer or winter, and on both sides of the US switch (2026-11-01).
    for target, send, expire in (('2026-07-15', '2026-07-14T23:05:00Z', '2026-07-14T23:55:00Z'),   # US daylight time
                                 ('2026-12-15', '2026-12-14T23:05:00Z', '2026-12-14T23:55:00Z'),   # US standard time
                                 ('2026-10-30', '2026-10-29T23:05:00Z', '2026-10-29T23:55:00Z'),   # before the US switch
                                 ('2026-11-03', '2026-11-02T23:05:00Z', '2026-11-02T23:55:00Z')):  # after it
        item = only(build(snapshot(current_row('down', target=target)), f'{target}T06:00:00'))
        assert item['sendAfter'] == send and item['expireAt'] == expire, (target, item['sendAfter'], item['expireAt'])
        assert item['id'] == f'kr_forecast:{target}' and item['open']['targetSession'] == target, item
    # the instants really are 08:05 and 08:55 in Seoul
    item = only(build(snapshot(current_row('down')), '2026-11-06T06:00:00'))
    assert datetime.fromisoformat(item['sendAfter'].replace('Z', '+00:00')).astimezone(KST).strftime('%H:%M') == '08:05'
    assert datetime.fromisoformat(item['expireAt'].replace('Z', '+00:00')).astimezone(KST).strftime('%H:%M') == '08:55'


def check_weekend_and_holiday_send_nothing() -> None:
    for target in ('2026-11-07', '2026-11-08',        # Saturday, Sunday
                   '2026-10-09', '2026-12-25'):       # KRX holidays (Hangul Day, Christmas)
        assert build(snapshot(current_row('down', target=target)), f'{target}T07:00:00') == [], target
        assert build(snapshot(current_row('down', target=target, state='closed_scored', actual_open=-2.0,
                                          actual_close=-3.0, hit=True)), f'{target}T15:50:00') == [], target
    # a year the holiday table does not cover is not guessed at: nothing, and no exception
    assert po.build_items(snapshot(current_row('down', target='2031-03-04')), at('2031-03-04T06:00:00')) == []


def check_saturday_built_monday() -> None:
    # Fri 2026-11-06 US session -> the Monday forecast is issued on Saturday morning; the alert waits for Monday 08:05.
    block = mfc.run('2026-11-07T07:00:00')
    cur = block['current']
    assert cur['targetSession'] == '2026-11-09' and cur['state'] == 'issued', cur
    snap = {'morningForecast': block}
    item = only(build(snap, '2026-11-07T07:00:00', 'saturday for monday'))
    assert item['id'] == 'kr_forecast:2026-11-09', item['id']
    assert item['sendAfter'] == '2026-11-08T23:05:00Z' and item['expireAt'] == '2026-11-08T23:55:00Z', item   # Mon 08:05 / 08:55
    assert item['open'] == {'screen': 'home', 'focus': 'forecast', 'targetSession': '2026-11-09'}, item['open']
    assert item['title']['ko'] == '꽁치 예보 · 오늘 한국장', item['title']   # sent on Monday, so "today" is right when it lands
    # Sunday night it is still listed, a minute after the window it is not
    assert ids(build(snap, '2026-11-09T07:59:00')) == ['kr_forecast:2026-11-09']
    assert build(snap, '2026-11-09T08:55:00') == []


def check_expiry() -> None:
    snap = snapshot(current_row('down'))
    assert ids(build(snap, '2026-11-05T20:00:00')) == ['kr_forecast:2026-11-06'], 'listed ahead of its send time'
    assert ids(build(snap, '2026-11-06T08:54:59')) == ['kr_forecast:2026-11-06'], 'still valid one second before 08:55'
    assert build(snap, '2026-11-06T08:55:00') == [], 'dropped at 08:55 (expireAt is exclusive)'
    assert build(snap, '2026-11-06T09:30:00') == [], 'no "forecast" after the market opened'
    # the same day once the open is scored, 08:55 has passed
    scored = snapshot(current_row('down', state='open_scored', actual_open=-2.2, hit=True))
    assert build(scored, '2026-11-06T09:40:00') == []
    # result: valid until 17:59:59, dropped at 18:00
    done = snapshot(current_row('down', state='closed_scored', actual_open=-2.24, actual_close=-6.02, hit=True), card=scorecard(15, 14))
    first = only(build(done, '2026-11-06T15:50:00'))
    assert first['id'] == 'kr_result:2026-11-06' and first['expireAt'] == '2026-11-06T09:00:00Z', first   # 18:00 Seoul
    assert first['sendAfter'] == '2026-11-06T06:50:00Z', first['sendAfter']                               # the moment it was made
    assert ids(build(done, '2026-11-06T17:59:59')) == ['kr_result:2026-11-06']
    assert build(done, '2026-11-06T18:00:00') == []
    assert build(done, '2026-11-07T07:00:00') == [], 'yesterday\'s result is never sent the next morning'


# ---------------------------------------------------------------- which days send what

def check_topics() -> None:
    call = only(build(snapshot(current_row('down')), '2026-11-06T06:00:00', 'call day'))
    assert call['hasCall'] is True and call['kind'] == 'kr_forecast', call
    assert call['topics'] == ['kr_forecast_call_ko', 'kr_forecast_daily_ko', 'kr_forecast_call_en', 'kr_forecast_daily_en'], call['topics']
    none = only(build(snapshot(current_row('none', spy=0.21, soxx=-0.33)), '2026-11-06T06:00:00', 'no-call day'))
    assert none['hasCall'] is False and none['topics'] == ['kr_forecast_daily_ko', 'kr_forecast_daily_en'], none['topics']
    assert not any('call' in topic for topic in none['topics']), 'a no-call day never goes to the call topics'
    res = only(build(snapshot(current_row('down', state='closed_scored', actual_open=-2.24, actual_close=-6.02, hit=True)),
                     '2026-11-06T15:50:00', 'result'))
    assert res['hasCall'] is True and res['topics'] == ['kr_result_ko', 'kr_result_en'], res['topics']
    assert res['open'] == {'screen': 'home', 'focus': 'scorecard', 'targetSession': '2026-11-06'}, res['open']
    # every topic name is one the Worker and the app agree on, and each language gets its own topics
    for item in (call, none, res):
        assert all(po.TOPIC_PATTERN.match(topic) for topic in item['topics']), item['topics']
        assert {t[-2:] for t in item['topics']} == {'ko', 'en'}
    # a day with a call is also listed to the daily topic: a device on "daily" gets it, a device on "call" gets it, and
    # the app subscribes to one of the two, so nobody gets it twice (the app's own contract enforces the choice)
    assert call['topics'].count('kr_forecast_daily_ko') == 1 and call['topics'].count('kr_forecast_call_ko') == 1


def check_state_gates() -> None:
    # An issued day lists the morning alert only; the result needs the scored close.
    issued = build(snapshot(current_row('down')), '2026-11-06T06:00:00')
    assert ids(issued) == ['kr_forecast:2026-11-06'], issued
    open_scored = build(snapshot(current_row('down', state='open_scored', actual_open=-2.2, hit=True)), '2026-11-06T09:40:00')
    assert open_scored == [], 'no result alert before the close is scored'
    open_scored_early = build(snapshot(current_row('down', state='open_scored', actual_open=-2.2, hit=True)), '2026-11-06T08:30:00')
    assert ids(open_scored_early) == ['kr_forecast:2026-11-06'], 'the morning alert is not lost to a state change'
    # no-call day: scored, but a result alert only goes out for a day with a call
    no_call_scored = snapshot(current_row('none', state='closed_scored', actual_open=-0.4, actual_close=-1.1, hit=None))
    assert build(no_call_scored, '2026-11-06T15:50:00') == [], 'a no-call day has no result alert'
    # a day without a forecast, a void day, a backfilled row, nothing at all
    for state in ('no_forecast', 'void', 'pending'):
        row = current_row('down')
        row.update(state=state, predicted=None)
        assert build(snapshot(row), '2026-11-06T06:00:00') == [], state
    assert build(snapshot(current_row('down', origin='backfill')), '2026-11-06T06:00:00') == [], 'backfill is never sent'
    assert build(snapshot(None), '2026-11-06T06:00:00') == []
    assert build({'generatedAt': '2026-11-06T00:00:00Z'}, '2026-11-06T06:00:00') == [], 'no morningForecast at all'


def check_paused_and_unavailable() -> None:
    for status in ('paused', 'unavailable'):
        assert build(snapshot(current_row('down'), status=status), '2026-11-06T06:00:00') == [], status
        assert build(snapshot(current_row('down', state='closed_scored', actual_open=-2.0, actual_close=-3.0, hit=True), status=status),
                     '2026-11-06T15:50:00') == [], status
    assert build(snapshot(current_row('down', paused=True)), '2026-11-06T06:00:00') == [], 'a paused row sends nothing'


def check_one_id_per_day() -> None:
    snap = snapshot(current_row('down'))
    first = [item['id'] for item in build(snap, '2026-11-06T05:30:00')]
    again = [item['id'] for item in build(snap, '2026-11-06T07:40:00')]
    assert first == again == ['kr_forecast:2026-11-06'], (first, again)
    # the result keeps its id every run until it expires, so the Worker's record (id + topic) stops a second send
    done = snapshot(current_row('down', state='closed_scored', actual_open=-2.24, actual_close=-6.02, hit=True), card=scorecard(15, 14))
    runs = [only(build(done, f'2026-11-06T{hhmm}:00')) for hhmm in ('15:50', '16:30', '17:30')]
    assert {item['id'] for item in runs} == {'kr_result:2026-11-06'} and len({item['expireAt'] for item in runs}) == 1, runs
    # one id per date and kind: a forecast and a result of the same day differ only by kind
    assert 'kr_forecast:2026-11-06' != 'kr_result:2026-11-06'
    # a different day is a different id
    assert only(build(snapshot(current_row('down', target='2026-11-09')), '2026-11-09T06:00:00'))['id'] == 'kr_forecast:2026-11-09'


def check_cap() -> None:
    items = [{'id': f'kr_forecast:2026-11-{day:02d}', 'sendAfter': f'2026-11-{day:02d}T23:00:00Z'} for day in range(1, 8)]
    kept = po.limit_items(items[::-1])
    assert len(kept) == po.MAX_ITEMS == 4 and kept == items[:4], kept
    block = {'schemaVersion': po.SCHEMA_VERSION, 'generatedAt': None, 'items': [deepcopy(sample_item()) for _ in range(5)]}
    try:
        po.assert_public_push_outbox(block)
    except AssertionError:
        return
    raise AssertionError('more than four items must be refused')


# ---------------------------------------------------------------- copy (SPEC §13.4)

def check_copy_examples() -> None:
    # 방향 낸 날
    item = only(build(snapshot(current_row('down'), card=scorecard(14, 13)), '2026-11-06T06:00:00', 'call, strong down'))
    assert item['title'] == {'ko': '꽁치 예보 · 오늘 한국장', 'en': 'Ggongchi forecast · Korea today'}, item['title']
    assert item['body']['ko'] == '크게 밀려서 시작해요. 코스피 시가 −3% 안팎 · 최근 14번 중 13번 적중', item['body']['ko']
    assert item['body']['en'] == 'Korea is set to open sharply lower. KOSPI open around −3% · 13 of last 14 calls right', item['body']['en']
    # 방향 없는 날
    item = only(build(snapshot(current_row('none', spy=0.21, soxx=-0.33)), '2026-11-06T06:00:00', 'no call'))
    assert item['body']['ko'] == '오늘은 방향을 내지 않아요. 밤사이 S&P500 +0.2%, 미국 반도체 −0.3%', item['body']
    assert item['body']['en'] == 'No call today. Overnight: S&P 500 +0.2%, US chips −0.3%', item['body']
    # 채점: 적중
    done = current_row('down', state='closed_scored', actual_open=-2.24, actual_close=-6.02, hit=True)
    item = only(build(snapshot(done, card=scorecard(15, 14)), '2026-11-06T15:50:00', 'result, hit'))
    assert item['title'] == {'ko': '꽁치 예보 채점 · 적중', 'en': 'Ggongchi forecast result · Hit'}, item['title']
    assert item['body']['ko'] == '시가 −2.2%(예보 −3% 안팎), 종가 −6.0%. 최근 15번 중 14번 적중', item['body']['ko']
    assert item['body']['en'] == 'Open −2.2% (forecast around −3%), close −6.0%. 14 of last 15 calls right', item['body']['en']
    # 채점: 빗나감 (예보는 하락, 시가는 올라서 시작) - sent the same way: a missed day is not hidden
    missed = current_row('down', state='closed_scored', actual_open=0.4, actual_close=-1.0, hit=False)
    item = only(build(snapshot(missed, card=scorecard(15, 13)), '2026-11-06T15:50:00', 'result, miss'))
    assert item['title'] == {'ko': '꽁치 예보 채점 · 빗나감', 'en': 'Ggongchi forecast result · Miss'}, item['title']
    assert item['body']['ko'] == '예보는 하락 출발, 실제 시가 +0.4%. 최근 15번 중 13번 적중', item['body']['ko']
    assert item['body']['en'] == 'Called a lower open; actual open +0.4%. 13 of last 15 calls right', item['body']['en']
    # the miss of an up call reads the other way round
    up_missed = current_row('up', open_pct=1.4, state='closed_scored', actual_open=-0.6, actual_close=-1.2, hit=False)
    item = only(build(snapshot(up_missed, card=scorecard(12, 9)), '2026-11-06T15:50:00', 'result, up miss'))
    assert item['body']['ko'] == '예보는 상승 출발, 실제 시가 −0.6%. 최근 12번 중 9번 적중', item['body']['ko']


def check_copy_variants() -> None:
    # a moderate call: the first sentence is the card title after its comma, word for word ("가능성이 높아요" included)
    down = only(build(snapshot(current_row('down', open_pct=-0.6)), '2026-11-06T06:00:00', 'moderate down'))
    assert down['body']['ko'] == '내려서 시작할 가능성이 높아요. 코스피 시가 −0.5% 안팎 · 최근 14번 중 13번 적중', down['body']['ko']
    assert down['body']['en'].startswith('Korea is likely to open lower. KOSPI open around −0.5%'), down['body']
    up = only(build(snapshot(current_row('up', open_pct=0.8)), '2026-11-06T06:00:00', 'moderate up'))
    assert up['body']['ko'].startswith('올라서 시작할 가능성이 높아요.') and up['body']['en'].startswith('Korea is likely to open higher.'), up['body']
    big_up = only(build(snapshot(current_row('up', open_pct=2.4)), '2026-11-06T06:00:00', 'strong up'))
    assert big_up['body']['ko'].startswith('크게 올라서 시작해요.') and big_up['body']['en'].startswith('Korea is set to open sharply higher.')
    # no real forecast yet: the clause is left out, the rest stays
    quiet = only(build(snapshot(current_row('down'), card=scorecard(0, 0, live_sessions=0)), '2026-11-06T06:00:00', 'no record'))
    assert quiet['body']['ko'] == '크게 밀려서 시작해요. 코스피 시가 −3% 안팎', quiet['body']['ko']
    assert quiet['body']['en'] == 'Korea is set to open sharply lower. KOSPI open around −3%', quiet['body']['en']
    broken = only(build(snapshot(current_row('down'), card={'recent': {'calls': 3, 'openHits': 5}}), '2026-11-06T06:00:00', 'bad record'))
    assert '최근' not in broken['body']['ko'], 'a record that does not add up is not printed'
    gone = only(build(snapshot(current_row('down'), card=None), '2026-11-06T06:00:00', 'default card'))
    assert gone['body']['ko'].startswith('크게 밀려서 시작해요'), gone
    # a call whose size reads "보합권" still prints
    flat = only(build(snapshot(current_row('down', open_pct=-0.35)), '2026-11-06T06:00:00', 'flat-sized call'))
    assert '보합권' in flat['body']['ko'], flat['body']
    # a result with no close number leaves the close out
    no_close = current_row('down', state='closed_scored', actual_open=-2.24, actual_close=None, hit=True)
    item = only(build(snapshot(no_close), '2026-11-06T15:50:00', 'result without close'))
    assert '종가' not in item['body']['ko'], item['body']


def check_record_piece() -> None:
    """SPEC §13.4 supplement: the record piece counts real forecasts only."""
    down = current_row('down')
    done = current_row('down', state='closed_scored', actual_open=-2.24, actual_close=-6.02, hit=True)
    miss = current_row('down', state='closed_scored', actual_open=0.4, actual_close=-1.0, hit=False)
    # 0 real forecasts (the recent window is all recomputed): no piece, no stray ' · ' or full stop
    zero = scorecard(14, 13, live_sessions=0, live_calls=0, live_hits=0)
    body = only(build(snapshot(down, card=zero), '2026-11-06T06:00:00', 'record: zero live'))['body']
    assert body == {'ko': '크게 밀려서 시작해요. 코스피 시가 −3% 안팎', 'en': 'Korea is set to open sharply lower. KOSPI open around −3%'}, body
    body = only(build(snapshot(done, card=zero), '2026-11-06T15:50:00', 'record: zero live, hit'))['body']
    assert body == {'ko': '시가 −2.2%(예보 −3% 안팎), 종가 −6.0%', 'en': 'Open −2.2% (forecast around −3%), close −6.0%'}, body
    body = only(build(snapshot(miss, card=zero), '2026-11-06T15:50:00', 'record: zero live, miss'))['body']
    assert body == {'ko': '예보는 하락 출발, 실제 시가 +0.4%', 'en': 'Called a lower open; actual open +0.4%'}, body
    # fewer than 5 live calls: still no piece, whatever the recomputed window says
    four = scorecard(14, 13, live_sessions=4, live_calls=4, live_hits=4)
    assert '적중' not in only(build(snapshot(down, card=four), '2026-11-06T06:00:00', 'record: 4 live'))['body']['ko']
    # 5 or more live calls but the 20 days are not all live: "지금까지" (the recent window is NOT quoted)
    some = scorecard(14, 13, live_sessions=9, live_calls=6, live_hits=5)
    body = only(build(snapshot(down, card=some), '2026-11-06T06:00:00', 'record: so far'))['body']
    assert body['ko'] == '크게 밀려서 시작해요. 코스피 시가 −3% 안팎 · 지금까지 6번 중 5번 적중', body['ko']
    assert body['en'] == 'Korea is set to open sharply lower. KOSPI open around −3% · 5 of 6 calls right so far', body['en']
    body = only(build(snapshot(done, card=some), '2026-11-06T15:50:00', 'record: so far, hit'))['body']
    assert body['ko'] == '시가 −2.2%(예보 −3% 안팎), 종가 −6.0%. 지금까지 6번 중 5번 적중', body['ko']
    assert body['en'] == 'Open −2.2% (forecast around −3%), close −6.0%. 5 of 6 calls right so far', body['en']
    body = only(build(snapshot(miss, card=some), '2026-11-06T15:50:00', 'record: so far, miss'))['body']
    assert body['ko'] == '예보는 하락 출발, 실제 시가 +0.4%. 지금까지 6번 중 5번 적중', body['ko']
    # exactly 5 is enough, a live total that does not add up is not printed
    five = scorecard(14, 13, live_sessions=5, live_calls=5, live_hits=3)
    assert '지금까지 5번 중 3번 적중' in only(build(snapshot(down, card=five), '2026-11-06T06:00:00', 'record: five'))['body']['ko']
    odd = scorecard(14, 13, live_sessions=5, live_calls=5, live_hits=7)
    assert '적중' not in only(build(snapshot(down, card=odd), '2026-11-06T06:00:00', 'record: odd'))['body']['ko']
    # all 20 recent sessions are real forecasts: "최근" with the recent window's numbers (not the running totals)
    full = scorecard(14, 13, live_sessions=20, live_calls=40, live_hits=35)
    body = only(build(snapshot(down, card=full), '2026-11-06T06:00:00', 'record: recent'))['body']
    assert body['ko'] == '크게 밀려서 시작해요. 코스피 시가 −3% 안팎 · 최근 14번 중 13번 적중', body['ko']
    assert body['en'].endswith('13 of last 14 calls right'), body['en']
    # all live but nothing to count in the window (no call in 20 days): no piece
    empty = scorecard(0, 0, live_sessions=20, live_calls=0, live_hits=0)
    assert '적중' not in only(build(snapshot(down, card=empty), '2026-11-06T06:00:00', 'record: empty'))['body']['ko']
    # a missing live block: only the recent window can speak, and only when it is all live
    bare = {'recent': scorecard(14, 13, live_sessions=19)['recent']}
    assert '적중' not in only(build(snapshot(down, card=bare), '2026-11-06T06:00:00', 'record: bare'))['body']['ko']


def check_lead_is_the_card_title() -> None:
    """The first sentence of a call-day body equals the card title after '한국장, ' (ko) and the title sentence (en)."""
    for call, size in (('down', -3.22), ('down', -0.6), ('up', 0.8), ('up', 3.22)):
        row = current_row(call, open_pct=size)
        item = only(build(snapshot(row), '2026-11-06T06:00:00', f'lead {call} {size}'))
        title = row['copy']['title']
        assert item['body']['ko'].split('. ')[0] == title['ko'].split('한국장, ')[1], (item['body']['ko'], title)
        assert item['body']['en'].split('. ')[0] == title['en'], (item['body']['en'], title)
    # a card made for a later day (Saturday, for Monday) says "월요일 한국장, ..." / "Monday: ..."; the lead is the same sentence
    monday = current_row('down', open_pct=-0.6, target='2026-11-09')
    monday['copy']['title'] = {'ko': '월요일 한국장, 내려서 시작할 가능성이 높아요', 'en': 'Monday: Korea is likely to open lower'}
    item = only(build(snapshot(monday), '2026-11-07T07:00:00', 'lead for monday'))
    assert item['body']['ko'].startswith('내려서 시작할 가능성이 높아요. ') and item['body']['en'].startswith('Korea is likely to open lower. '), item['body']
    # no title on the card: no alert (the lead is never invented)
    broken = current_row('down')
    broken['copy'].pop('title')
    assert build(snapshot(broken), '2026-11-06T06:00:00') == []
    # a no-call day keeps its fixed sentence
    none = only(build(snapshot(current_row('none', spy=0.21, soxx=-0.33)), '2026-11-06T06:00:00', 'lead no call'))
    assert none['body']['ko'].startswith('오늘은 방향을 내지 않아요. ') and none['body']['en'].startswith('No call today. ')
    # the allowed phrase passes the copy check only as "가능성이 높아요"
    ok = deepcopy(only(build(snapshot(current_row('down', open_pct=-0.6)), '2026-11-06T06:00:00')))
    assert not po.item_problems(ok), po.item_problems(ok)
    for text in ('내려서 시작할 가능성도 있어요. 코스피 시가 −0.5% 안팎', '내려서 시작할 가능성이 있어요. 코스피 시가 −0.5% 안팎'):
        bad = deepcopy(ok)
        bad['body']['ko'] = text
        assert po.item_problems(bad), text
    assert '것 같아요' not in ok['body']['ko']


def check_copy_rules_all() -> None:
    assert len(ALL_ITEMS) >= 20, len(ALL_ITEMS)
    for label, item in ALL_ITEMS:
        problems = po.item_problems(item)
        assert not problems, (label, problems)
        assert len(item['title']['ko']) <= po.LIMIT_TITLE_KO == 20 and len(item['body']['ko']) <= po.LIMIT_BODY_KO == 60, (label, item)
        assert '!' not in item['body']['ko'] + item['body']['en'], label
    # the real builder's copy for every strength and direction, through the real copy words
    for call, size in (('down', -3.22), ('down', -0.6), ('up', 0.8), ('up', 3.22)):
        item = only(build(snapshot(current_row(call, open_pct=size)), '2026-11-06T06:00:00', f'{call} {size}'))
        assert not po.item_problems(item), item


def check_copy_rules_negative() -> None:
    good = only(build(snapshot(current_row('down')), '2026-11-06T06:00:00'))

    def with_text(field: str, lang: str, text: str) -> dict:
        item = deepcopy(good)
        item[field][lang] = text
        return item

    bad = [
        with_text('body', 'ko', '크게 밀려서 시작해요. 코스피 시가 −3% 안팎 · 다음 갱신에서 확인해요'),   # banned 확인 / 다음 갱신
        with_text('body', 'ko', '크게 밀려서 시작해요. 지금 사세요 −3%'),                              # an instruction to trade
        with_text('body', 'ko', '크게 밀려서 시작해요. 매도 −3% 안팎'),                                 # a bare 매도
        with_text('body', 'ko', '크게 밀려서 시작해요. 삼성전자 −3% 안팎'),                              # a stock name
        with_text('body', 'ko', '크게 밀려서 시작할 가능성도 있어요. −3%'),                              # banned 가능성
        with_text('body', 'ko', '크게 밀려서 시작해요! 코스피 시가 −3% 안팎'),                            # exclamation
        with_text('body', 'ko', '크게 밀려서 시작해요. 코스피 시가가 크게 밀려서 시작한다고 봅니다 숫자 1' + '가' * 40),   # too long
        with_text('body', 'ko', '크게 밀려서 시작해요. 코스피 시가는 하락합니다'),                         # no number, 합쇼체
        with_text('body', 'ko', '밀려서 시작합니다. 코스피 시가 −3% 안팎입니다.'),                        # 합쇼체
        with_text('body', 'ko', '코스피 시가 −3% 안팎 · 최근 14번 중 13번 적중'),                          # lead is not a 해요체 sentence with a direction
        with_text('title', 'ko', '꽁치 예보 · 오늘 한국장은 크게 내려요 정말로'),                           # title over 20
        with_text('body', 'en', 'Set to open sharply lower. You should buy the dip, KOSPI −3%'),
        with_text('body', 'en', 'Watch the KOSPI open around −3%'),
        with_text('body', 'en', 'Korea may open lower, KOSPI −3%'),
        with_text('body', 'en', 'Set to open sharply lower'),                                          # no number
        with_text('body', 'en', 'Set to open sharply lower. Samsung −3%'),
        with_text('title', 'en', ''),
    ]
    for item in bad:
        assert po.item_problems(item), item
    # the net-flow words stay allowed, as in the coach brief
    ok = with_text('body', 'ko', '크게 밀려서 시작해요. 외국인 순매도 2.8조 원')
    assert not po.item_problems(ok), po.item_problems(ok)
    # an item with a copy problem is dropped, the others are kept
    poisoned = current_row('down', state='closed_scored', actual_open=-2.24, actual_close=-6.02, hit=True)
    poisoned['copy']['openStat'] = {'ko': '확인 −3%', 'en': 'around −3%'}
    out = po.build_items(snapshot(poisoned, card=scorecard(15, 14)), at('2026-11-06T15:50:00'))
    assert out == [], 'both items quote the poisoned openStat'
    chips_poisoned = current_row('none', spy=0.2, soxx=-0.3)
    chips_poisoned['copy']['chips'][0]['ko'] = 'S&P500 점검 +0.2%'
    assert po.build_items(snapshot(chips_poisoned), at('2026-11-06T06:00:00')) == []
    # one item with a copy problem is dropped, the other stays (a closed day lists a morning item and a result item)
    both = snapshot(current_row('down', state='closed_scored', actual_open=-2.24, actual_close=-6.02, hit=True), card=scorecard(15, 14))
    original = po.morning_copy
    try:
        po.morning_copy = lambda *_a: (po.TITLE_MORNING, po.pair('지금 사세요 −3%', 'Set to open lower −3%'))
        # a fixture with a scored close seen at 08:30 is not a real moment, but it holds both windows open at once
        assert ids(po.build_items(both, at('2026-11-06T08:30:00'))) == ['kr_result:2026-11-06'], \
            'the morning item with a banned word is dropped, the clean result item stays'
    finally:
        po.morning_copy = original


# ---------------------------------------------------------------- never raises, public shape

def check_never_raises() -> None:
    garbage = [None, {}, {'morningForecast': None}, {'morningForecast': 'x'}, {'morningForecast': {'status': 'ok'}},
               {'morningForecast': {'status': 'ok', 'current': 'x'}}, {'morningForecast': {'status': 'ok', 'current': {}}},
               {'morningForecast': {'status': 'ok', 'current': {'origin': 'live', 'state': 'issued', 'targetSession': 'nope'}}},
               {'morningForecast': {'status': 'ok', 'current': {'origin': 'live', 'state': 'issued', 'targetSession': '2026-11-06',
                                                                  'predicted': {'openCall': 'down'}, 'copy': {}}}},
               {'morningForecast': {'status': 'ok', 'current': {'origin': 'live', 'state': 'closed_scored', 'targetSession': '2026-11-06',
                                                                  'predicted': {'openCall': 'down'}, 'actual': 'x', 'result': []}}},
               {'morningForecast': {'status': 'ok', 'current': {'origin': 'live', 'state': 'issued', 'targetSession': '2026-11-06',
                                                                  'predicted': {'openCall': 'none'}, 'copy': {'chips': [1, 2]}}}}]
    for snap in garbage:
        moment = at('2026-11-06T06:00:00')
        out = po.apply_push_outbox(snap, moment)   # must not raise
        if isinstance(snap, dict):
            assert out['pushOutbox']['items'] == [] and out['pushOutbox']['schemaVersion'] == po.SCHEMA_VERSION, snap
            po.assert_public_push_outbox(out['pushOutbox'])
    # a bad clock and a broken builder do not raise either, and still leave a well-formed empty block
    snap = snapshot(current_row('down'))
    for bad_now in ('not a time', 12345, object()):
        out = po.apply_push_outbox(deepcopy(snap), bad_now)
        assert out['pushOutbox']['items'] == [], bad_now
        po.assert_public_push_outbox(out['pushOutbox'])
    original = po.build_items
    try:
        def boom(*_args, **_kw):
            raise RuntimeError('boom')
        po.build_items = boom
        out = po.apply_push_outbox(deepcopy(snap), at('2026-11-06T06:00:00'))
        assert out['pushOutbox']['items'] == [], out
        po.assert_public_push_outbox(out['pushOutbox'])
    finally:
        po.build_items = original
    # the happy path through the entry point
    out = po.apply_push_outbox(deepcopy(snap), at('2026-11-06T06:00:00'))
    assert ids(out['pushOutbox']['items']) == ['kr_forecast:2026-11-06'] and out['pushOutbox']['generatedAt'] == '2026-11-05T21:00:00Z'
    # a naive clock is taken as UTC, like the other layers
    assert po.apply_push_outbox(deepcopy(snap), datetime(2026, 11, 5, 21, 0))['pushOutbox']['generatedAt'] == '2026-11-05T21:00:00Z'


def sample_item() -> dict:
    return only(build(snapshot(current_row('down')), '2026-11-06T06:00:00', 'sample item'))


def sample_outbox() -> dict:
    """The 2026-11-06 06:45 outbox from the real forecast block (for the security contract)."""
    block = mfc.run('2026-11-06T06:45:00')
    return po.apply_push_outbox({'morningForecast': block}, at('2026-11-06T06:45:00'))['pushOutbox']


def check_public_shape() -> None:
    outbox = sample_outbox()
    assert ids(outbox['items']) == ['kr_forecast:2026-11-06'], outbox
    po.assert_public_push_outbox(outbox)
    json.dumps(outbox, ensure_ascii=False, allow_nan=False)
    # absent or null passes; a block that is there is checked
    po.assert_public_push_outbox_if_present({})
    po.assert_public_push_outbox_if_present({'pushOutbox': None})
    po.assert_public_push_outbox_if_present({'pushOutbox': {'schemaVersion': po.SCHEMA_VERSION, 'generatedAt': None, 'items': []}})
    item = outbox['items'][0]

    def broken(**changes) -> dict:
        mutated = deepcopy(item)
        mutated.update(changes)
        return {'pushOutbox': {'schemaVersion': po.SCHEMA_VERSION, 'generatedAt': outbox['generatedAt'], 'items': [mutated]}}

    cases = [
        {'pushOutbox': 'x'}, {'pushOutbox': {}}, {'pushOutbox': {'schemaVersion': 'other', 'items': []}},
        {'pushOutbox': {'schemaVersion': po.SCHEMA_VERSION, 'generatedAt': 'yesterday', 'items': []}},
        {'pushOutbox': {'schemaVersion': po.SCHEMA_VERSION, 'generatedAt': None, 'items': 'x'}},
        {'pushOutbox': {'schemaVersion': po.SCHEMA_VERSION, 'generatedAt': None, 'items': [item, item]}},   # duplicate id
        broken(id='kr_forecast:today'), broken(id='other:2026-11-06'), broken(kind='kr_result'),
        broken(topics=[]), broken(topics=['kr_forecast_call_ko', 'kr_forecast_call_ko']),
        broken(topics=['news_breaking_ko', 'news_breaking_en']),                      # not a kr_ topic
        broken(topics=['kr_forecast_call_ko', 'kr_forecast_daily_ko']),              # no English topic
        broken(topics=['kr_result_ko', 'kr_result_en']),                              # a forecast item on result topics
        broken(topics=['kr_forecast_daily_ko', 'kr_forecast_daily_en', 'kr_forecast_daily_xx']),
        broken(hasCall=False),                                                        # call topics on a no-call item
        broken(hasCall='yes'), broken(title={'ko': 'a'}), broken(body={'ko': '', 'en': 'x'}),
        broken(sendAfter='2026-11-05 23:00'), broken(expireAt=item['sendAfter']),
        broken(open={'screen': 'home', 'focus': 'forecast', 'targetSession': '2026-11-09'}),
        broken(open={'screen': 'settings', 'focus': 'forecast', 'targetSession': '2026-11-06'}),
        broken(open='home'),
    ]
    for case in cases:
        try:
            po.assert_public_push_outbox_if_present(case)
        except AssertionError:
            continue
        raise AssertionError(f'the shape check accepted {case!r}')


def check_evening_prepared_not_listed() -> None:
    """The evening notification is built and passes every copy rule, but is never listed while the switch is off."""
    block = ebc.sample_evening_block()
    snap = {'eveningBrief': block}
    issued = datetime.fromisoformat(block['current']['issuedAt'].replace('Z', '+00:00'))
    assert po.EVENING_PUSH_LISTED is False, 'the evening notification stays unlisted until 형 turns it on'
    assert po.build_items(snap, issued) == [], 'switch off: nothing listed'
    item = po.evening_item(snap, issued)
    assert item and not po.item_problems(item), (item, item and po.item_problems(item))
    assert item['id'] == 'us_evening:2026-11-10' and item['topics'] == ['kr_us_evening_ko', 'kr_us_evening_en']
    assert item['sendAfter'] == block['current']['issuedAt'] and item['expireAt'] == '2026-11-10T14:30:00Z'   # 09:30 New York
    assert item['body']['ko'].startswith('강세 출발 구간이에요. S&P 선물 +0.60%'), item['body']
    assert item['open'] == {'screen': 'home', 'focus': 'evening', 'targetSession': '2026-11-10'}
    po.assert_public_push_outbox({**po.empty_block(issued), 'items': [item]})
    assert po.evening_item(snap, datetime.fromisoformat('2026-11-10T14:30:00+00:00')) is None, 'dropped at the US open'
    quiet = deepcopy(block)
    quiet['current']['call'] = 'none'
    assert po.evening_item({'eveningBrief': quiet}, issued) is None, 'no notification on a night without a clear start'
    po.EVENING_PUSH_LISTED = True
    try:
        assert ids(po.build_items(snap, issued)) == ['us_evening:2026-11-10']
    finally:
        po.EVENING_PUSH_LISTED = False


def check_wired_after_coach_brief() -> None:
    import polarmeter_v3_inputs as v3_inputs
    source = inspect.getsource(v3_inputs.apply_v3_layers)
    assert source.index('apply_coach_brief') < source.index('apply_push_outbox'), 'the outbox comes after the coach brief'


# ---------------------------------------------------------------- samples

def write_samples(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    examples = {}
    for name, label in (('call', 'call, strong down'), ('no_call', 'no call'), ('hit', 'result, hit'), ('miss', 'result, miss'),
                         ('call_moderate', 'moderate down'), ('call_so_far', 'record: so far'), ('call_no_record', 'record: zero live')):
        examples[name] = next(item for text, item in reversed(ALL_ITEMS) if text == label)
    (directory / 'push-outbox-examples.json').write_text(json.dumps(examples, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (directory / 'push-outbox-sample.json').write_text(json.dumps(sample_outbox(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples-dir', type=Path, default=None)
    args = parser.parse_args()
    check_send_time_ignores_daylight_saving()
    check_weekend_and_holiday_send_nothing()
    check_saturday_built_monday()
    check_expiry()
    check_topics()
    check_state_gates()
    check_paused_and_unavailable()
    check_one_id_per_day()
    check_cap()
    check_copy_examples()
    check_copy_variants()
    check_record_piece()
    check_lead_is_the_card_title()
    check_copy_rules_negative()
    check_copy_rules_all()
    check_never_raises()
    check_public_shape()
    check_wired_after_coach_brief()
    check_evening_prepared_not_listed()
    if args.samples_dir:
        write_samples(args.samples_dir)
    print('PASS push outbox: 08:05 Seoul time all year, weekends and holidays, topics by day type, 08:55 and 18:00 expiry, '
          'no result before the scored close, paused / unavailable / no forecast, one id per day, copy (ko/en) and §6.5, '
          'miss day, cap, never raises, public shape (1-D)')


if __name__ == '__main__':
    main()
