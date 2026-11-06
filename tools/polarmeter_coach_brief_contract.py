#!/usr/bin/env python3
"""Offline contract: the coach brief (꽁치 조언), SPEC §4.6, §6.4-§6.6, bundle 1-C.

Fixed in-code data, no network. Covers the five time zones against rules C0-C13, the order and the floor of the
evidence, the baseline numbers L1-L5, the schedule (both sides of the US daylight-saving switch), every §6.5 copy
rule in both languages, dropped pieces, the whole card dropping out, and never raising.

  PYTHONPATH=tools python3 tools/polarmeter_coach_brief_contract.py [--samples-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import math
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polarmeter_coach_brief as cb
import polarmeter_morning_forecast as mf
import polarmeter_morning_forecast_contract as mfc

KST = ZoneInfo('Asia/Seoul')
ALL_BRIEFS: list[tuple[str, dict]] = []   # every brief the cases produce; the copy-rule pass at the end reads all of them


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=KST)


def utc(text: str) -> str:
    return text if text.endswith('Z') else text + 'Z'


# ---------------------------------------------------------------- fixtures

def forecast_row(call: str = 'down', *, state: str = 'issued', target: str = '2026-11-06', basis: str = '2026-11-05',
                 sessions: list[str] | None = None, spy: float | None = -4.41, soxx: float | None = -12.15,
                 excess: float | None = -8.18, top: str = 'ewyExcessPct', open_pct: float | None = None,
                 close_pct: float | None = None, hit: bool | None = None) -> dict:
    """A `morningForecast.current` shaped like the real one (only what the coach reads)."""
    inputs = None if spy is None else {'spyPct': spy, 'soxxPct': soxx, 'ewyPct': -13.46, 'kospiPrevPct': -5.28, 'ewyExcessPct': excess}
    size = {'down': -3.22, 'up': 3.22, 'none': 0.1}[call]
    row = {'targetSession': target, 'basisKrSession': basis, 'usSessions': sessions if sessions is not None else [basis],
           'origin': 'live', 'state': state, 'issuedAt': '2026-11-05T21:45:00Z', 'noForecastReason': None, 'inputs': inputs,
           'predicted': {'openPct': size, 'closePct': size * 0.9, 'openBand': [size - 1, size + 1],
                         'strength': 'weak' if call == 'none' else 'strong', 'openCall': call,
                         'closeCall': call, 'topDriver': top},
           'analog': None, 'actual': None, 'result': None}
    if state == 'no_forecast':
        row.update(predicted=None, inputs=None, noForecastReason='us_market_closed')
    if open_pct is not None:
        row['actual'] = {'openPct': open_pct, 'closePct': close_pct, 'intradayPct': None, 'source': 'naver_index_price'}
        row['result'] = {'openHit': hit, 'closeHit': None, 'openInBand': None}
    return row


def morning_block(row: dict | None, phase: str, *, status: str = 'ok', next_issue: str = '2026-11-06T21:45:00Z') -> dict:
    return {'schemaVersion': mf.SCHEMA_VERSION, 'modelVersion': mf.MODEL_VERSION, 'generatedAt': '2026-11-06T00:00:00Z',
            'status': status, 'phase': phase, 'current': row, 'ledger': [], 'scorecard': None, 'nextIssueAt': next_issue,
            'disclaimer': mf.DISCLAIMER}


def signal(value: float, change_pct: float, as_of: str, previous: float | None = None, status: str = 'ok') -> dict:
    return {'status': status, 'value': value, 'changePct': change_pct, 'previousClose': previous, 'dataAsOf': utc(as_of)}


def flows_ledger(last_day: str = '2026-11-05', sells: int = 2, last_net: tuple[float, float] = (-26892.0, -896.0),
                 settled: bool = True) -> dict:
    """29 alternating +-13,000 days, then (`sells` - 1) days of -15,000, then `last_day` with `last_net`.
    With sells=2: typical day = sqrt((29 * 13000^2 + 15000^2) / 30) = 13,071.6 and the last day is 2.13x that."""
    day = date.fromisoformat(last_day)
    days = []
    while len(days) < 29 + sells - 1:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    days.reverse()
    items = []
    for i, d in enumerate(days):
        net = -15000.0 if i >= 29 else (13000.0 if i % 2 == 0 else -13000.0)
        items.append({'date': d.isoformat(), 'kospi': {'foreign': net, 'institution': 0, 'personal': 0},
                      'kosdaq': {'foreign': 0.0, 'institution': 0, 'personal': 0}, 'settled': True})
    if sells == 1:  # make yesterday a buying day so the streak is just one
        items[-1]['kospi']['foreign'] = 15000.0
    items.append({'date': last_day, 'kospi': {'foreign': last_net[0], 'institution': 0, 'personal': 0},
                  'kosdaq': {'foreign': last_net[1], 'institution': 0, 'personal': 0}, 'settled': settled})
    return {'version': 'investor-flows-v1', 'status': 'ok', 'unit': 'KRW_100M', 'items': items}


def score(us=(48.0, 30.0), kr=(33.0, 21.0)) -> dict:
    def pair(p):
        return {'currentScore': p[1], 'previousScore': p[0], 'delta': p[1] - p[0]}
    return {'status': 'ok', 'dailyDelta': {'us': pair(us), 'kr': pair(kr)}}


def macro(key: str, label: str, scheduled_at: str) -> dict:
    return {key: {'status': 'ok', 'lastRelease': None, 'nextRelease': {'scheduledAt': scheduled_at, 'label': label}}}


def snapshot(block: dict | None, signals: dict | None = None, *, flows: dict | None = None, temps: dict | None = None,
             macros: dict | None = None, link: list | None = None) -> dict:
    out: dict = {'generatedAt': '2026-11-06T00:00:00Z'}
    if block is not None:
        out['morningForecast'] = block
    out['signals'] = signals if signals is not None else {}
    out['investorFlows'] = flows if flows is not None else flows_ledger()
    out['scoreCoreV3'] = temps if temps is not None else score()
    out['macroEvents'] = macros if macros is not None else {}
    out['linkSignals'] = link if link is not None else []
    return out


def morning_signals(**over) -> dict:
    """The 2026-11-06 06:50 numbers: won 1,358 (+1.3%), VIX 31 (+18%). Pass key=None to remove a signal."""
    base = {'usd_krw': signal(1358.0, (1358.0 / 1340.0 - 1) * 100, '2026-11-05T21:40:00Z', 1340.0),
            'vix': signal(31.0, 18.0, '2026-11-05T21:00:00Z', 26.27),
            'sp500': signal(711.0, -4.41, '2026-11-05T21:00:00Z', 743.8)}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def build(snap: dict, now: str, label: str = '') -> dict | None:
    brief = cb.build_coach_brief(snap, at(now))
    if brief is not None:
        ALL_BRIEFS.append((label or now, brief))
    return brief


def ko(brief: dict) -> str:
    return brief['verdict']['ko']


# ---------------------------------------------------------------- 1. verdicts: time zones x rules

PRE_NOW = '2026-11-06T06:50:00'


def pre_snap(call='down', **row_args) -> dict:
    return snapshot(morning_block(forecast_row(call, **row_args), 'pre_open'), morning_signals())


def check_pre_open_rules() -> None:
    cases = [
        # (label, call, row args, rule, tone, ko, en)
        ('C1 down, S&P500 leads', 'down', dict(), 'C1', 'minus', '미국 급락이 그대로 넘어오는 날이에요',
         'The US plunge carries straight over to Korea'),
        ('C2 down, chips lead', 'down', dict(spy=-1.0, soxx=-3.0, excess=-1.0, top='soxxPct'), 'C2', 'minus',
         '미국 반도체가 한국장을 끌어내리는 날이에요', 'US chips are pulling Korea lower'),
        ('C3 down, Korea alone', 'down', dict(spy=-0.5, soxx=-1.0, excess=-5.0), 'C3', 'minus',
         '밤사이 한국 주식만 따로 팔렸어요', 'Korean stocks alone were dumped overnight'),
        ('C0 down', 'down', dict(spy=-0.5, soxx=-1.0, excess=-0.5, top='spyPct'), 'C0', 'minus',
         '밤사이 미국장 약세가 넘어오는 날이에요', 'US weakness carries over to Korea overnight'),
        ('C4 up, S&P500 leads', 'up', dict(spy=1.6, soxx=1.0, excess=1.0, top='spyPct'), 'C4', 'plus',
         '미국 급등이 그대로 넘어오는 날이에요', 'The US surge carries straight over to Korea'),
        ('C5 up, chips lead', 'up', dict(spy=1.0, soxx=2.6, excess=1.0, top='soxxPct'), 'C5', 'plus',
         '미국 반도체가 한국장을 끌어올리는 날이에요', 'US chips are pulling Korea higher'),
        ('C6 up, Korea alone', 'up', dict(spy=0.5, soxx=1.0, excess=5.0), 'C6', 'plus',
         '밤사이 한국 주식만 따로 샀어요', 'Korean stocks alone were bid up overnight'),
        ('C0 up', 'up', dict(spy=0.5, soxx=1.0, excess=0.5, top='spyPct'), 'C0', 'plus',
         '밤사이 미국장 강세가 넘어오는 날이에요', 'US strength carries over to Korea overnight'),
        ('C7 no call', 'none', dict(spy=0.2, soxx=-0.3, excess=0.1, top='spyPct'), 'C7', 'neutral',
         '미국장은 조용했어요. 한국 안쪽 재료가 정해요', 'US stocks were quiet. Korea-specific news decides'),
    ]
    for label, call, args, rule, tone, text_ko, text_en in cases:
        brief = build(pre_snap(call, **args), PRE_NOW, label)
        assert brief is not None, label
        assert (brief['ruleId'], brief['tone'], ko(brief), brief['verdict']['en']) == (rule, tone, text_ko, text_en), (label, brief['verdict'])
        assert brief['phase'] == 'pre_open' and brief['schemaVersion'] == cb.SCHEMA_VERSION
        assert brief['generatedAt'] == '2026-11-05T21:50:00Z', brief['generatedAt']
    # Order: the first row that fits wins. S&P500 at exactly 1.5% is C1 even when chips are even stronger.
    assert build(pre_snap(spy=-1.5, soxx=-9.0), PRE_NOW)['ruleId'] == 'C1'
    assert build(pre_snap(spy=-1.49, soxx=-2.5, excess=-0.4, top='soxxPct'), PRE_NOW)['ruleId'] == 'C2'
    assert build(pre_snap(spy=-1.49, soxx=-2.49, excess=-6.0), PRE_NOW)['ruleId'] == 'C3'
    # A Korea-ETF driver that points against the call is not "Korea alone sold off".
    assert build(pre_snap(spy=-0.5, soxx=-1.0, excess=3.0), PRE_NOW)['ruleId'] == 'C0'
    assert build(pre_snap('up', spy=0.5, soxx=1.0, excess=-3.0), PRE_NOW)['ruleId'] == 'C0'
    # Up thresholds are the mirror of the down ones.
    assert build(pre_snap('up', spy=1.5, soxx=0.0, excess=0.0, top='spyPct'), PRE_NOW)['ruleId'] == 'C4'
    assert build(pre_snap('up', spy=1.49, soxx=2.5, excess=0.0, top='soxxPct'), PRE_NOW)['ruleId'] == 'C5'
    # Missing inputs fall through to the generic sentence instead of failing.
    assert build(pre_snap(spy=None), PRE_NOW) is not None and build(pre_snap(spy=None), PRE_NOW)['ruleId'] == 'C0'
    # The week-start morning (Monday 06:50, target today) is an ordinary pre-open card.
    monday = snapshot(morning_block(forecast_row(target='2026-11-09', basis='2026-11-06', sessions=['2026-11-06']), 'pre_open'),
                      morning_signals())
    assert build(monday, '2026-11-09T06:50:00', 'monday')['ruleId'] == 'C1'


def open_snap(call='down', open_pct=-2.24, hit=True, kospi_pct=-3.5, kospi_as_of='2026-11-06T00:50:00', prev=5383.8,
              state='open_scored', **over) -> dict:
    row = forecast_row(call, state=state, open_pct=open_pct, hit=hit)
    signals = morning_signals(kospi=signal(prev * (1 + kospi_pct / 100), kospi_pct, kospi_as_of, prev) if kospi_pct is not None else None)
    signals.update(over)
    return snapshot(morning_block(row, 'kr_open'), signals)


OPEN_NOW = '2026-11-06T10:05:00'


def check_kr_open_rules() -> None:
    # Down call, the open hit, the index now below its open: C8. (0.965 / 0.9776 - 1 = -1.29%)
    brief = build(open_snap(), OPEN_NOW, 'C8 down')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C8', 'minus', '예보대로 시작해 시가보다 1.3% 더 내려왔어요'), brief['verdict']
    assert brief['verdict']['en'] == 'As called, 1.3% further below the open'
    # Same open, the index back above it: C9 (0.99 / 0.9776 - 1 = +1.26%)
    brief = build(open_snap(kospi_pct=-1.0), OPEN_NOW, 'C9 down')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C9', 'plus', '밀려서 시작했지만 시가보다 1.3% 올라왔어요'), brief['verdict']
    assert brief['verdict']['en'] == 'Opened lower but now 1.3% above the open'
    # Up call mirrors it. (1.03 / 1.02 - 1 = +0.98%; 1.01 / 1.02 - 1 = -0.98%)
    brief = build(open_snap('up', open_pct=2.0, kospi_pct=3.0), OPEN_NOW, 'C8 up')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C8', 'plus', '예보대로 시작해 시가보다 1.0% 더 올라왔어요')
    brief = build(open_snap('up', open_pct=2.0, kospi_pct=1.0), OPEN_NOW, 'C9 up')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C9', 'minus', '올라서 시작했지만 시가보다 1.0% 내려왔어요')
    assert brief['verdict']['en'] == 'Opened higher but now 1.0% below the open'
    # Exactly at the open: neither continued nor reversed.
    brief = build(open_snap(kospi_pct=-2.24), OPEN_NOW, 'at the open')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C9', 'neutral', '밀려서 시작해 지금은 시가와 비슷해요'), brief['verdict']
    # No fresh KOSPI quote (before the open, a previous-day quote, or a feed stuck for hours): still says the open hit.
    for label, kwargs in (('no quote', dict(kospi_pct=None)), ('yesterday', dict(kospi_as_of='2026-11-05T06:30:00')),
                          ('before 09:00', dict(kospi_as_of='2026-11-05T23:50:00')),
                          ('stuck 3h', dict(kospi_as_of='2026-11-05T22:50:00'))):
        brief = build(open_snap(**kwargs), OPEN_NOW, f'C8 {label}')
        assert (brief['ruleId'], ko(brief)) == ('C8', '예보대로 밀려서 시작했어요'), (label, brief['verdict'])
    # C10: the open went against the call. The verdict names the way it actually opened.
    brief = build(open_snap(open_pct=0.4, hit=False, kospi_pct=0.9), OPEN_NOW, 'C10 up against down')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C10', 'plus', '밤사이 신호와 달리 올라서 시작했어요'), brief['verdict']
    assert brief['verdict']['en'] == 'Opened higher, against the overnight signal'
    brief = build(open_snap('up', open_pct=-0.4, hit=False, kospi_pct=-0.9), OPEN_NOW, 'C10 down against up')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C10', 'minus', '밤사이 신호와 달리 내려서 시작했어요')
    brief = build(open_snap(open_pct=0.0, hit=False, kospi_pct=None), OPEN_NOW, 'C10 flat open')  # an exact 0 is a miss
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C10', 'neutral', '밤사이 신호와 달리 내려서 시작하지 않았어요')
    # Before the open is scored, and on a no-call day, the morning sentence still describes the day.
    assert build(open_snap(state='issued', open_pct=None, hit=None), '2026-11-06T09:02:00')['ruleId'] == 'C1'
    nocall = snapshot(morning_block(forecast_row('none', spy=0.2, soxx=-0.3, excess=0.1, top='spyPct', state='open_scored', open_pct=-0.4), 'kr_open'),
                      morning_signals())
    assert build(nocall, OPEN_NOW, 'C7 in session')['ruleId'] == 'C7'
    # No forecast that day: nothing to say about it.
    dead = snapshot(morning_block(forecast_row('down', state='no_forecast'), 'kr_open'), morning_signals())
    assert build(dead, OPEN_NOW) is None


def close_snap(close_pct: float | None = -6.02, kospi_pct: float | None = -6.02, kospi_as_of='2026-11-06T07:00:00',
               now_phase='kr_closed', row: dict | None = None, **over) -> dict:
    row = row or forecast_row(state='closed_scored', open_pct=-2.24, close_pct=close_pct, hit=True)
    signals = morning_signals(kospi=signal(5059.69, kospi_pct, kospi_as_of, 5383.8) if kospi_pct is not None else None)
    signals.update(over)
    return snapshot(morning_block(row, now_phase), signals)


CLOSE_NOW = '2026-11-06T16:10:00'


def check_kr_closed_rules() -> None:
    brief = build(close_snap(), CLOSE_NOW, 'C11 down')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C11', 'minus', '오늘 코스피 6.0% 내려 마감했어요'), brief['verdict']
    assert brief['verdict']['en'] == 'KOSPI closed 6.0% lower today'
    # The scored close wins over the quote; without it the post-close quote is used; a mid-session quote is not a close.
    assert ko(build(close_snap(close_pct=-5.0, kospi_pct=-6.02), CLOSE_NOW)) == '오늘 코스피 5.0% 내려 마감했어요'
    assert ko(build(close_snap(close_pct=None, kospi_pct=-6.02), CLOSE_NOW)) == '오늘 코스피 6.0% 내려 마감했어요'
    assert build(close_snap(close_pct=None, kospi_pct=-6.02, kospi_as_of='2026-11-06T05:30:00'), CLOSE_NOW) is None  # 14:30 KST
    assert build(close_snap(close_pct=None, kospi_pct=None), CLOSE_NOW) is None
    brief = build(close_snap(close_pct=1.234), CLOSE_NOW, 'C11 up')
    assert (brief['tone'], ko(brief)) == ('plus', '오늘 코스피 1.2% 올라 마감했어요') and brief['verdict']['en'] == 'KOSPI closed 1.2% higher today'
    brief = build(close_snap(close_pct=0.03), CLOSE_NOW, 'C11 flat')
    assert (brief['tone'], ko(brief)) == ('neutral', '오늘 코스피는 조용히 마감했어요')
    # After midnight the card is still about the session that closed: it names the date, not "오늘".
    late = build(close_snap(), '2026-11-07T00:30:00', 'C11 after midnight')
    assert ko(late) == '11월 6일 코스피 6.0% 내려 마감했어요' and late['verdict']['en'] == 'KOSPI closed 6.0% lower on Nov 6'
    # The close needs no forecast: a no-forecast day still gets its closing sentence.
    assert build(close_snap(row=forecast_row(state='no_forecast'), close_pct=None), CLOSE_NOW, 'C11 no forecast')['ruleId'] == 'C11'
    # The SPEC's second half (foreign net selling) is an evidence line; KOSPI itself is not repeated there.
    brief = build(close_snap(), CLOSE_NOW)
    assert 'kospi' not in [e['key'] for e in brief['evidence']]
    assert not any(e['key'] == 'kospi' for e in brief['evidence'])


US_NOW = '2026-11-06T23:40:00'


def us_snap(spx=-1.2, soxx=-2.0, as_of='2026-11-06T14:30:00', **over) -> dict:
    signals = morning_signals(sp500=signal(700.0, spx, as_of) if spx is not None else None,
                              soxx=signal(130.0, soxx, as_of) if soxx is not None else None)
    signals.update(over)
    return snapshot(morning_block(forecast_row(state='closed_scored', open_pct=-2.24, close_pct=-6.02, hit=True), 'us_open'), signals)


def check_us_open_rules() -> None:
    brief = build(us_snap(), US_NOW, 'C12 both down')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C12', 'minus', 'S&P500 1.2%·반도체 2.0% 내려요'), brief['verdict']
    assert brief['verdict']['en'] == 'S&P 500 down 1.2%, US chips down 2.0%'
    brief = build(us_snap(0.8, 1.5), US_NOW, 'C12 both up')
    assert (brief['tone'], ko(brief)) == ('plus', 'S&P500 0.8%·반도체 1.5% 올라요')
    # Different directions, or no chip quote: S&P500 alone.
    assert ko(build(us_snap(0.8, -1.5), US_NOW)) == '미국장 S&P500이 0.8% 올라 있어요'
    assert ko(build(us_snap(-1.2, None), US_NOW)) == '미국장 S&P500이 1.2% 내려 있어요'
    # Even two-digit moves keep the paired sentence inside 28 characters (the S&P500-only fallback guards the length).
    wide = build(us_snap(-12.34, -20.1), US_NOW, 'C12 wide')
    assert ko(wide) == 'S&P500 12.3%·반도체 20.1% 내려요' and len(ko(wide)) <= cb.LIMIT_VERDICT
    flat = build(us_snap(0.02, 0.5), US_NOW, 'C12 flat')
    assert (flat['tone'], ko(flat)) == ('neutral', '미국장 S&P500은 조용해요')
    # No S&P500 quote, or one older than 3 hours: no card.
    assert build(us_snap(None, None), US_NOW) is None
    assert build(us_snap(as_of='2026-11-06T09:30:00'), US_NOW) is None


WEEKEND_NOW = '2026-11-07T10:00:00'


def weekend_snap(spy: float | None = 0.8, sessions=('2026-11-06',), call='down', **over) -> dict:
    row = forecast_row(call, target='2026-11-09', basis='2026-11-06', sessions=list(sessions), spy=spy, soxx=0.2, excess=0.1,
                       top='spyPct')
    signals = morning_signals()
    signals.update(over)
    return snapshot(morning_block(row, 'pre_open', next_issue='2026-11-09T21:45:00Z'), signals,
                    flows=flows_ledger('2026-11-06'))


def check_weekend_rules() -> None:
    brief = build(weekend_snap(), WEEKEND_NOW, 'C13 up')
    assert (brief['ruleId'], brief['tone'], ko(brief)) == ('C13', 'plus', '금요일 S&P500은 0.8% 올라 끝났어요'), brief['verdict']
    assert brief['verdict']['en'] == 'S&P 500 ended 0.8% higher on Friday'
    brief = build(weekend_snap(-0.8), WEEKEND_NOW, 'C13 down')
    assert (brief['tone'], ko(brief)) == ('minus', '금요일 S&P500은 0.8% 내려 끝났어요')
    brief = build(weekend_snap(0.04), WEEKEND_NOW, 'C13 flat')
    assert (brief['tone'], ko(brief)) == ('neutral', '금요일 S&P500은 조용히 끝났어요')
    brief = build(weekend_snap(0.8, sessions=('2026-11-05', '2026-11-06')), WEEKEND_NOW, 'C13 two sessions')
    assert ko(brief) == '지난 S&P500은 0.8% 올라 끝났어요' and brief['verdict']['en'] == 'S&P 500 ended 0.8% higher in the last US session'
    assert build(weekend_snap(None), WEEKEND_NOW) is None  # no inputs on the row: nothing to say
    # The weekend card does not depend on the call: a no-call Monday gets the same kind of sentence.
    assert build(weekend_snap(call='none'), WEEKEND_NOW, 'C13 no call')['ruleId'] == 'C13'
    # A weekday holiday is the same card: Friday 2026-10-09 (한글날), Monday's forecast not yet out -> last row is Thursday's.
    holiday = snapshot(morning_block(forecast_row(target='2026-10-12', basis='2026-10-09', sessions=['2026-10-08', '2026-10-09'],
                                                  spy=1.1, soxx=0.1, excess=0.0, top='spyPct'), 'pre_open',
                                     next_issue='2026-10-12T21:45:00Z'), morning_signals(), flows=flows_ledger('2026-10-08'))
    assert build(holiday, '2026-10-10T10:00:00', 'holiday')['ruleId'] == 'C13'


# ---------------------------------------------------------------- 2. evidence

def check_evidence() -> None:
    # 2026-11-06 06:50: US 48->30 (3.6), VIX 31 (+18%: 3.6), won +1.34% (3.36), KR 33->21 (2.4), foreign 2.13x.
    brief = build(pre_snap(), PRE_NOW, 'evidence base')
    assert [e['key'] for e in brief['evidence']] == ['us_temp', 'vix', 'usd_krw'], brief['evidence']   # tie 3.6 = 3.6: list order
    assert [e['ko'] for e in brief['evidence']] == ['미국 온도 48° → 30°', 'VIX 31(+18%)', '원/달러 1,358원(+1.3%)']
    assert [e['en'] for e in brief['evidence']] == ['US temperature 48° → 30°', 'VIX 31(+18%)', 'USD/KRW 1,358 (+1.3%)']
    assert brief['evidenceCount'] == 5
    # Without the VIX move the next one in line comes in; the order follows the strength.
    brief = build(snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(vix=signal(18.0, 2.0, '2026-11-05T21:00:00Z'))),
                  PRE_NOW, 'evidence no vix')
    assert [e['key'] for e in brief['evidence']] == ['us_temp', 'usd_krw', 'kr_temp'], brief['evidence']
    assert brief['evidence'][2]['ko'] == '한국 온도 33° → 21°' and brief['evidenceCount'] == 4
    # Anything under strength 1 is left out: temps 4.9 / 4.9 degrees, won +0.39%, VIX +4.9% at 24, foreign 0.9x.
    quiet = snapshot(morning_block(forecast_row(), 'pre_open'),
                     morning_signals(usd_krw=signal(1345.0, 0.39, '2026-11-05T21:40:00Z', 1339.8), vix=signal(24.0, 4.9, '2026-11-05T21:00:00Z')),
                     temps=score((50.0, 45.1), (30.0, 34.9)), flows=flows_ledger(last_net=(-11700.0, 0.0), sells=1))
    brief = build(quiet, PRE_NOW, 'evidence quiet')
    assert brief['evidence'] == [] and brief['evidenceCount'] == 0, brief['evidence']      # the 근거 line is simply empty
    assert brief['verdict'] and brief['lines'] and brief['next'], 'the rest of the card stays'
    # The edge: exactly strength 1 counts. Temp 5 degrees, won 0.4%.
    edge = snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(usd_krw=signal(1345.0, 0.4, '2026-11-05T21:40:00Z', 1339.64), vix=None),
                    temps=score((50.0, 45.0), (30.0, 34.9)), flows=flows_ledger(last_net=(-100.0, 0.0), sells=1))
    assert [e['key'] for e in build(edge, PRE_NOW, 'evidence edge')['evidence']] == ['us_temp', 'usd_krw']
    # VIX counts by level too: 26, barely moving, is 26 / 25 = 1.04.
    level = snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(vix=signal(26.0, 0.5, '2026-11-05T21:00:00Z'), usd_krw=None),
                     temps=score((50.0, 50.0), (30.0, 30.0)), flows=flows_ledger(last_net=(-100.0, 0.0), sells=1))
    brief = build(level, PRE_NOW, 'evidence vix level')
    assert [e['ko'] for e in brief['evidence']] == ['VIX 26(0%)'], brief['evidence']
    # Foreign flow: the streak form (이틀째 / 사흘째 / 나흘째, then 5일째); the single-day form; the buying side.
    only_flow = dict(usd_krw=None, vix=None)
    flow_snap = lambda ledger: snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(**only_flow), flows=ledger,
                                        temps=score((50.0, 50.0), (30.0, 30.0)))
    streak = build(flow_snap(flows_ledger()), PRE_NOW, 'flow streak')['evidence']
    assert [e['ko'] for e in streak] == ['외국인 이틀째 순매도'] and streak[0]['en'] == 'Foreign net selling, day 2 (2.8T won)'
    typical = math.sqrt((29 * 13000 ** 2 + 15000 ** 2) / 30)           # the same day-size rule `link_signals` uses
    assert abs(typical - 13071.6) < 0.1 and abs(27788 / typical - 2.126) < 0.001
    single = build(flow_snap(flows_ledger(sells=1)), PRE_NOW, 'flow single')['evidence']
    assert single[0]['ko'] == '외국인 2.8조 원 순매도(평소의 2.1배)' and len(single[0]['ko']) <= cb.LIMIT_EVIDENCE
    assert single[0]['en'] == 'Foreign net selling 2.8T won (2.1x usual)'
    buy = build(flow_snap(flows_ledger(sells=2, last_net=(26892.0, 896.0))), PRE_NOW, 'flow buy')['evidence']
    assert buy[0]['ko'] == '외국인 2.8조 원 순매수(평소의 2.1배)' and buy[0]['en'] == 'Foreign net buying 2.8T won (2.1x usual)'
    small_unit = build(flow_snap(flows_ledger(last_net=(-26000.0, 0.0), sells=3)), PRE_NOW)['evidence']
    assert small_unit[0]['ko'] == '외국인 사흘째 순매도'
    assert build(flow_snap(flows_ledger(last_net=(-26000.0, 0.0), sells=4)), PRE_NOW)['evidence'][0]['ko'] == '외국인 나흘째 순매도'
    assert build(flow_snap(flows_ledger(last_net=(-26000.0, 0.0), sells=5)), PRE_NOW)['evidence'][0]['ko'] == '외국인 5일째 순매도'
    # Flows of another day than the card's are not used; the published linkSignals row is the fallback for the same day.
    assert build(flow_snap(flows_ledger('2026-11-04')), PRE_NOW) is not None
    assert build(flow_snap(flows_ledger('2026-11-04')), PRE_NOW)['evidence'] == []
    row = {'key': 'foreign_flow', 'value': '-27,788억', 'changePct': None, 'asOf': '2026-11-05', 'readKo': '외국인 순매도 · 평소 규모의 1.4배'}
    fallback = snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(**only_flow), temps=score((50.0, 50.0), (30.0, 30.0)),
                        flows={'items': []}, link=[row])
    assert build(fallback, PRE_NOW, 'flow fallback')['evidence'][0]['ko'] == '외국인 2.8조 원 순매도(평소의 1.4배)'
    # KOSPI is evidence only while the session is on or just closed; strength is |change| / 1%.
    brief = build(open_snap(), OPEN_NOW, 'evidence kospi open')
    assert [e['key'] for e in brief['evidence']] == ['us_temp', 'vix', 'kospi'], brief['evidence']      # 3.6, 3.6, then 3.5
    assert brief['evidence'][2] == {'key': 'kospi', 'ko': '코스피 −3.5%', 'en': 'KOSPI −3.5%'}, brief['evidence']
    assert all(e['key'] != 'kospi' for e in build(pre_snap(), PRE_NOW)['evidence'])
    assert all(e['key'] != 'kospi' for e in build(weekend_snap(), WEEKEND_NOW)['evidence'])
    # A missing signal is a missing piece, not a missing card.
    bare = snapshot(morning_block(forecast_row(), 'pre_open'), {}, flows={}, temps={})
    brief = build(bare, PRE_NOW, 'bare')
    assert brief['evidence'] == [] and brief['lines'] == [] and brief['ruleId'] == 'C1', brief
    # Every evidence item stays inside 24 characters and carries a digit.
    for label, item in ((l, e) for l, b in ALL_BRIEFS for e in b['evidence']):
        assert len(item['ko']) <= cb.LIMIT_EVIDENCE and any(c.isdigit() for c in item['ko'] + item['en']), (label, item)


# ---------------------------------------------------------------- 3. baselines

def lines_of(snap: dict, now: str, label: str = '') -> list[dict]:
    return build(snap, now, label or f'lines {now}')['lines']


def check_baselines() -> None:
    lines = lines_of(pre_snap(), PRE_NOW, 'L1 L2')
    # 1,340.0 x 1.004 = 1,345.36 and x 0.996 = 1,334.64: the SPEC's 1,345 / 1,335. Won and VIX are both candidates; the won is closer.
    assert [l['ruleId'] for l in lines] == ['L1', 'L2'], lines
    assert lines[0] == {'ruleId': 'L1', 'level': {'ko': '1,345원 위', 'en': 'Above 1,345'}, 'tone': 'minus',
                        'ko': '원화 약세 구간이에요. 외국인 자금에 부담', 'en': 'Weak-won zone: a drag on foreign money'}, lines[0]
    assert lines[1] == {'ruleId': 'L2', 'level': {'ko': '1,335원 아래', 'en': 'Below 1,335'}, 'tone': 'plus',
                        'ko': '원화 강세 구간이에요. 외국인 자금에 우호', 'en': 'Strong-won zone: supportive of foreign money'}, lines[1]
    # The won far from its lines and VIX close to 30: the VIX pair.
    far = snapshot(morning_block(forecast_row(), 'pre_open'),
                   morning_signals(usd_krw=signal(1400.0, 4.5, '2026-11-05T21:40:00Z', 1340.0), vix=signal(29.5, 3.0, '2026-11-05T21:00:00Z')))
    lines = lines_of(far, PRE_NOW, 'L5 pair')
    assert lines == [{'ruleId': 'L5', 'level': {'ko': 'VIX 30 위', 'en': 'VIX above 30'}, 'tone': 'minus', 'ko': '공포가 큰 구간이에요',
                      'en': 'A high-fear zone'},
                     {'ruleId': 'L5', 'level': {'ko': 'VIX 20 아래', 'en': 'VIX below 20'}, 'tone': 'plus', 'ko': '평소 수준이에요',
                      'en': 'A calm zone'}], lines
    # No won quote: VIX only. No VIX either: no 기준선 line at all (the card stays).
    assert [l['ruleId'] for l in lines_of(snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(usd_krw=None)), PRE_NOW)] == ['L5', 'L5']
    assert lines_of(snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(usd_krw=None, vix=None)), PRE_NOW, 'no lines') == []
    # Without a previous close the won line is derived from the percent change; with neither it is not offered.
    no_prev = snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(usd_krw=signal(1358.0, 1.3433, '2026-11-05T21:40:00Z', None)))
    assert lines_of(no_prev, PRE_NOW)[0]['level']['ko'] == '1,345원 위'
    # Won from the published link row when the quote is missing.
    link = [{'key': 'usd_krw', 'value': '1,358.0원', 'changePct': 1.3433, 'asOf': '2026-11-05'}]
    from_link = snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(usd_krw=None), link=link)
    assert lines_of(from_link, PRE_NOW, 'link won')[0]['level']['ko'] == '1,345원 위'
    # L3 (open) exists only during the session. Open = 5,383.8 x (1 - 2.24%) = 5,263.2. Index below the open -> the "위" line.
    far_fx = dict(usd_krw=signal(1400.0, 4.5, '2026-11-06T00:30:00Z', 1340.0), vix=None)
    pick = lambda lines, rule: [l for l in lines if l['ruleId'] == rule][0]
    below = lines_of(open_snap(kospi_pct=-3.5, **far_fx), OPEN_NOW, 'L3 above')
    assert pick(below, 'L3') == {'ruleId': 'L3', 'level': {'ko': '5,263 위', 'en': 'Above 5,263'}, 'tone': 'plus',
                                 'ko': '시가보다 올라온 상태예요', 'en': 'Up from the open'}, below
    above = lines_of(open_snap(kospi_pct=-1.0, **far_fx), OPEN_NOW, 'L3 below')
    assert pick(above, 'L3') == {'ruleId': 'L3', 'level': {'ko': '5,263 아래', 'en': 'Below 5,263'}, 'tone': 'minus',
                                 'ko': '시가보다 더 내려온 상태예요', 'en': 'Down from the open'}, above
    # L3 is a single line (the side the index is not on), so the next closest line fills the second place - here the won.
    assert [l['ruleId'] for l in above] == ['L1', 'L3'], above
    assert all(l['ruleId'] != 'L3' for l in lines_of(pre_snap(), PRE_NOW)) and all(l['ruleId'] != 'L3' for l in lines_of(close_snap(), CLOSE_NOW))
    # L4 (foreign flow): only while the day is still building and below a typical day (typical 13,071.6 -> 1.3조 원).
    building = flows_ledger('2026-11-06', sells=2, last_net=(-9000.0, 0.0), settled=False)
    no_fx_vix = dict(usd_krw=None, vix=None)    # leave only the open and the foreign flow as candidates
    snap = open_snap(**no_fx_vix)
    snap['investorFlows'] = building
    lines = lines_of(snap, OPEN_NOW, 'L4 sell')
    l4 = [l for l in lines if l['ruleId'] == 'L4'][0]
    assert l4 == {'ruleId': 'L4', 'level': {'ko': '순매도 1.3조 원 넘으면', 'en': 'Net selling over 1.3T won'}, 'tone': 'minus',
                  'ko': '평소보다 큰 규모예요', 'en': 'Bigger than a typical day'}, l4
    snap['investorFlows'] = flows_ledger('2026-11-06', sells=2, last_net=(9000.0, 0.0), settled=False)
    assert [l for l in lines_of(snap, OPEN_NOW, 'L4 buy') if l['ruleId'] == 'L4'][0]['level']['ko'] == '순매수 1.3조 원 넘으면'
    for label, ledger in (('over a typical day', flows_ledger('2026-11-06', last_net=(-15000.0, 0.0), settled=False)),
                          ('settled', flows_ledger('2026-11-06', last_net=(-9000.0, 0.0), settled=True))):
        snap['investorFlows'] = ledger
        assert all(l['ruleId'] != 'L4' for l in lines_of(snap, OPEN_NOW, f'L4 off: {label}')), label
    closed = close_snap(**no_fx_vix)
    closed['investorFlows'] = flows_ledger('2026-11-06', last_net=(-9000.0, 0.0), settled=False)
    assert any(l['ruleId'] == 'L4' for l in lines_of(closed, CLOSE_NOW, 'L4 after the close'))
    pre = pre_snap()
    pre['investorFlows'] = flows_ledger('2026-11-05', last_net=(-9000.0, 0.0), settled=False)
    pre['signals'] = morning_signals(**far_fx)
    assert all(l['ruleId'] != 'L4' for l in lines_of(pre, PRE_NOW, 'L4 off before the open'))
    # Two lines at most, always; the display order is L1 < L2 < L3 < L4 < L5.
    for label, brief in ALL_BRIEFS:
        assert len(brief['lines']) <= 2, label
        assert [l['ruleId'] for l in brief['lines']] == sorted(l['ruleId'] for l in brief['lines']), label


# ---------------------------------------------------------------- 4. schedule

def next_of(snap: dict, now: str, label: str = '') -> list[dict]:
    return build(snap, now, label or f'next {now}')['next']


def check_schedule() -> None:
    # Winter (EST): the US opens 9:30 New York = 23:30 KST. Pre-open Friday: the Korean open and the first score.
    items = next_of(pre_snap(), PRE_NOW, 'next pre')
    assert [(i['atKst'], i['ko']) for i in items] == [('09:00', '한국장이 열려요'), ('09:35', '시가 예보를 채점해요')], items
    assert items[0]['at'] == '2026-11-06T00:00:00Z' and items[1]['at'] == '2026-11-06T00:35:00Z'
    # During the session, open scored: the close, then the US open at 23:30.
    items = next_of(open_snap(), OPEN_NOW, 'next kr_open')
    assert [(i['atKst'], i['ko']) for i in items] == [('15:30', '마감 뒤 오늘 예보를 채점해요'),
                                                      ('23:30', '미국장 시작, 반도체가 반등하는지 봐요')], items
    assert items[1]['at'] == '2026-11-06T14:30:00Z' and items[1]['en'] == 'US open, to see whether chips rebound'
    # After the close: the US open, then the next forecast (Saturday 06:45 = Friday's US close + the feed grace).
    items = next_of(close_snap(), CLOSE_NOW, 'next kr_closed')
    assert [(i['atKst'], i['ko']) for i in items] == [('23:30', '미국장 시작, 반도체가 반등하는지 봐요'), ('06:45', '내일 새 예보가 나와요')], items
    assert items[1]['at'] == '2026-11-06T21:45:00Z'
    # Late evening: only the forecast is left before tomorrow.
    items = next_of(us_snap(), US_NOW, 'next us_open')
    assert items[0]['ko'] == '내일 새 예보가 나와요' and items[0]['atKst'] == '06:45'
    # Summer (EDT): the same session is 22:30 KST. The switch days on both sides.
    summer_row = forecast_row(target='2026-07-15', basis='2026-07-14', state='open_scored', open_pct=-2.24, hit=True)
    summer = snapshot(morning_block(summer_row, 'kr_open', next_issue='2026-07-14T20:45:00Z'), morning_signals())
    items = next_of(summer, '2026-07-15T10:05:00', 'next summer')
    assert [(i['atKst'], i['at']) for i in items] == [('15:30', '2026-07-15T06:30:00Z'), ('22:30', '2026-07-15T13:30:00Z')], items
    assert items[1]['ko'] == '미국장 시작, 반도체가 반등하는지 봐요'
    for now, expect_kst, expect_utc in (('2026-03-06T10:05:00', '23:30', '2026-03-06T14:30:00Z'),    # Friday before DST starts (Mar 8)
                                        ('2026-03-09T10:05:00', '22:30', '2026-03-09T13:30:00Z'),    # Monday after
                                        ('2026-10-30T10:05:00', '22:30', '2026-10-30T13:30:00Z'),    # Friday before DST ends (Nov 1)
                                        ('2026-11-02T10:05:00', '23:30', '2026-11-02T14:30:00Z')):   # Monday after
        day = date.fromisoformat(now[:10]).isoformat()
        snap = snapshot(morning_block(forecast_row(target=day, basis='2026-01-01', state='open_scored', open_pct=-2.0, hit=True), 'kr_open',
                                      next_issue=f'{day}T21:45:00Z'), morning_signals())
        us_open = [i for i in next_of(snap, now, f'next dst {now[:10]}') if 'US' in i['en'] or '미국장' in i['ko']]
        assert us_open and (us_open[0]['atKst'], us_open[0]['at']) == (expect_kst, expect_utc), (now, us_open)
    # The US open text follows the call: up days and no-call days.
    up = open_snap('up', open_pct=2.0, kospi_pct=3.0)
    assert next_of(up, OPEN_NOW)[1]['ko'] == '미국장 시작, 반도체가 계속 오르는지 봐요'
    nocall = snapshot(morning_block(forecast_row('none', spy=0.1, soxx=0.1, excess=0.1, state='closed_scored', open_pct=-0.1), 'kr_closed'),
                      morning_signals(kospi=signal(5300.0, -1.5, '2026-11-06T07:00:00Z', 5383.8)))
    items = next_of(nocall, CLOSE_NOW, 'next no call')
    assert items[0]['ko'] == '미국장이 열려요' and items[0]['atKst'] == '23:30'
    # Weekend: Monday's open and first score; a no-call Monday scores nothing, so its close follows the open.
    items = next_of(weekend_snap(), WEEKEND_NOW, 'next weekend')
    assert [(i['atKst'], i['ko']) for i in items] == [('09:00', '월요일 한국장이 열려요'), ('09:35', '월요일 시가 예보를 채점해요')], items
    assert items[0]['at'] == '2026-11-09T00:00:00Z'
    items = next_of(weekend_snap(call='none'), WEEKEND_NOW, 'next weekend no call')
    assert [(i['atKst'], i['ko']) for i in items] == [('09:00', '월요일 한국장이 열려요'), ('15:30', '월요일 한국장이 마감해요')], items
    # A Korean holiday is skipped: 2026-10-09 (Fri) is shut, the next open is Monday.
    holiday = snapshot(morning_block(forecast_row(target='2026-10-12', basis='2026-10-09', sessions=['2026-10-08', '2026-10-09'],
                                                  spy=1.1, soxx=0.1, excess=0.0, top='spyPct'), 'pre_open', next_issue='2026-10-12T21:45:00Z'),
                       morning_signals(), flows=flows_ledger('2026-10-08'))
    items = next_of(holiday, '2026-10-09T10:00:00', 'next holiday')
    assert [(i['atKst'], i['ko']) for i in items] == [('22:30', '미국장이 열려요'), ('09:00', '월요일 한국장이 열려요')] or \
        [(i['atKst'], i['ko']) for i in items][1:] == [('09:00', '월요일 한국장이 열려요')], items   # no 09:00 / 15:30 on the shut Friday
    assert items[0]['atKst'] == '22:30' and items[0]['at'] == '2026-10-09T13:30:00Z'
    # A US holiday has no open: Thanksgiving Thursday 2026-11-26 (New York) is skipped, the next open is Friday 23:30 KST.
    thanks = snapshot(morning_block(forecast_row(target='2026-11-26', basis='2026-11-25', state='open_scored', open_pct=-1.0, hit=True), 'kr_open',
                                    next_issue='2026-11-30T21:45:00Z'), morning_signals())
    us_opens = [e for e in cb.schedule_events(cb.Context(thanks, at('2026-11-26T10:05:00'))) if e[1] == 1]
    assert mf.iso_utc(us_opens[0][0]) == '2026-11-27T14:30:00Z' and us_opens[0][2] == '내일 미국장 시작, 반도체가 반등하는지 봐요', us_opens[:2]
    assert not any(mf.iso_utc(e[0]).startswith('2026-11-26') for e in us_opens), us_opens
    # A sentence that would pass 26 characters with the weekday word falls back to the plain one (never drops the card).
    long_up = snapshot(morning_block(forecast_row('up', target='2026-11-09', basis='2026-11-06', sessions=['2026-11-06'], spy=1.6, soxx=1.0,
                                                  excess=1.0, top='spyPct', state='closed_scored', open_pct=2.0, close_pct=2.5, hit=True),
                                     'kr_closed', next_issue='2026-11-10T21:45:00Z'),
                       morning_signals(kospi=signal(5500.0, 2.5, '2026-11-09T07:00:00Z', 5365.0)))
    items = next_of(long_up, '2026-11-09T16:10:00', 'next long text')
    assert items[0]['ko'] == '미국장 시작, 반도체가 계속 오르는지 봐요', items
    with_day = [e for e in cb.schedule_events(cb.Context(long_up, at('2026-11-07T10:00:00'))) if e[1] == 1 and e[2].startswith('월요일')]
    assert with_day and with_day[0][2] == '월요일 미국장이 열려요', with_day
    # Releases: inside 24 hours only, after "now" only, labelled with the registry's own label. Same instant as the US open -> both.
    cpi = macro('us_cpi', '10월 CPI', '2026-11-06T14:30:00Z')
    items = next_of(snapshot(morning_block(forecast_row(state='closed_scored', open_pct=-2.24, close_pct=-6.02, hit=True), 'kr_closed'),
                             morning_signals(kospi=signal(5059.69, -6.02, '2026-11-06T07:00:00Z', 5383.8)), macros=cpi), CLOSE_NOW, 'next cpi')
    assert [(i['atKst'], i['ko']) for i in items] == [('23:30', '미국장 시작, 반도체가 반등하는지 봐요'), ('23:30', '10월 CPI 발표가 나와요')], items
    assert items[1]['en'] == 'US CPI report is out'
    for label, when in (('too far', '2026-11-07T17:00:00Z'), ('already out', '2026-11-06T06:00:00Z')):
        items = next_of(snapshot(morning_block(forecast_row(), 'pre_open'), morning_signals(), macros=macro('us_cpi', '10월 CPI', when)), PRE_NOW)
        assert all('CPI' not in i['ko'] for i in items), (label, items)
    # Nothing is "next" at the moment it happens.
    exactly = next_of(pre_snap(), '2026-11-06T09:00:00', 'next at 09:00')
    assert exactly[0]['atKst'] != '09:00', exactly
    for label, brief in ALL_BRIEFS:
        assert len(brief['next']) <= 2, label


# ---------------------------------------------------------------- 5. copy rules (SPEC §6.5)

def check_copy_rules_negative() -> None:
    """The checker itself: each rule must catch its own violation."""
    good = deepcopy(next(b for l, b in ALL_BRIEFS if l == 'C1 down, S&P500 leads'))
    assert cb.copy_problems(good) == [], cb.copy_problems(good)

    def bad(edit) -> list[str]:
        brief = deepcopy(good)
        edit(brief)
        return cb.copy_problems(brief)

    def setv(text):
        return lambda b: b['verdict'].update(ko=text)

    cases = {
        '금지 낱말 확인': (lambda b: b['evidence'][0].update(ko='미국 온도 48°를 확인해요'), 'banned 확인'),
        '금지 낱말 다음 갱신': (lambda b: b['next'][0].update(ko='다음 갱신에서 봐요'), 'banned 다음 갱신'),
        '금지 낱말 수 있어요': (setv('내려갈 수 있어요'), 'banned 수 있어요'),
        '매도하세요': (lambda b: b['lines'][0].update(ko='1,345원 위에서 매도하세요'), 'banned 매수/매도'),
        '매수 추천': (lambda b: b['lines'][0].update(ko='1,345원 아래 매수 추천이에요'), 'banned'),
        '순 없는 매수': (lambda b: b['lines'][0].update(ko='외국인 매수 구간이에요 1,345원'), 'banned 매수/매도'),
        '담으세요': (lambda b: b['lines'][0].update(ko='1,345원 아래에서 담으세요'), 'banned 담으세요'),
        '순매도하세요': (lambda b: b['lines'][0].update(ko='1,345원 위에서 매도하세요'), 'banned'),
        '비중': (lambda b: b['lines'][0].update(ko='비중을 줄여요'), 'banned 비중'),
        '종목 이름': (lambda b: b['evidence'][0].update(ko='삼성전자 -7.6%'), 'banned 삼성전자'),
        '방향 낱말 없음': (setv('미국이 그대로 넘어오는 날이에요'), 'direction word'),
        '28자 넘음': (setv('밤사이 미국장이 크게 내려서 한국장도 많이 밀려요 정말로 그래요요'), 'verdict over 28'),
        '해요체 아님': (setv('미국 급락이 밀려옴'), '해요체'),
        '근거에 숫자 없음': (lambda b: b['evidence'][0].update(ko='미국 온도가 내려갔어요'), 'evidence needs a number'),
        '근거 24자 넘음': (lambda b: b['evidence'][0].update(ko='미국 온도가 48도에서 30도로 아주 많이 내려갔어요 지금'), 'evidence over 24'),
        '기준선에 숫자 없음': (lambda b: b['lines'][0].update(level={'ko': '위', 'en': 'Above'}, ko='원화 약세 구간이에요', en='Weak-won zone'),
                         'line needs a number'),
        '기준선 뜻 30자 넘음': (lambda b: b['lines'][0].update(ko='원화 약세 구간이라서 외국인 자금에 꽤 큰 부담이 될 수밖에 없어요'), 'line meaning over 30'),
        '다음에 시각 없음': (lambda b: b['next'][0].update(atKst='아침'), 'next needs a time'),
        '다음 26자 넘음': (lambda b: b['next'][0].update(ko='미국장이 시작하면 반도체가 반등하는지 아닌지를 꼭 봐요 지금'), 'next over 26'),
        '영어 watch': (lambda b: b['verdict'].update(en='Watch the US drop'), 'banned English'),
        '영어 sold': (lambda b: b['evidence'][0].update(en='Foreigners sold 2.8T won'), 'banned English'),
        '영어 buying': (lambda b: b['evidence'][0].update(en='Foreigners buying 2.8T won'), 'banned English'),
        '영어 buy': (lambda b: b['lines'][0].update(en='Buy below 1,335'), 'banned English'),
        '영어 may': (lambda b: b['lines'][0].update(en='It may weigh on foreign money 1,345'), 'banned English'),
        '느낌표': (setv('미국 급락이 밀려와요!'), 'exclamation'),
        '이모지': (setv('미국 급락이 밀려와요 \U0001F4C9'), 'exclamation or emoji'),
        '빈 문장': (lambda b: b['evidence'][0].update(en=''), 'empty'),
    }
    for name, (edit, expect) in cases.items():
        problems = bad(edit)
        assert any(expect in p for p in problems), (name, problems)
    # The revised rules (SPEC §6.5, 2026-11-07): flows described as fact pass, instructions to trade do not.
    passing = {
        '순매도 근거': lambda b: b['evidence'][0].update(ko='외국인 2.8조 원 순매도(평소의 2.1배)', en='Foreign net selling 2.8T won'),
        '순매수 근거': lambda b: b['evidence'][0].update(ko='외국인 순매수 2.8조 원', en='Foreign net buying 2.8T won'),
        '이틀째 순매도': lambda b: b['evidence'][0].update(ko='외국인 이틀째 순매도', en='Foreign net selling, day 2'),
        '사흘째': lambda b: b['evidence'][0].update(ko='외국인 사흘째 순매도'),
        '나흘째': lambda b: b['evidence'][0].update(ko='외국인 나흘째 순매수'),
        '순매도 기준선': lambda b: b['lines'][0].update(level={'ko': '순매도 1.3조 원 넘으면', 'en': 'Net selling over 1.3T won'},
                                                  ko='평소보다 큰 규모예요', en='Bigger than a typical day'),
        '급락': setv('미국 급락이 그대로 넘어오는 날이에요'), '급등': setv('미국 급등이 그대로 넘어오는 날이에요'),
        '약세': setv('밤사이 미국장 약세가 넘어오는 날이에요'), '강세': setv('밤사이 미국장 강세가 넘어오는 날이에요'),
        '하락': setv('코스피 하락이 이어지는 날이에요'), '상승': setv('코스피 상승이 이어지는 날이에요'),
        '반등': setv('반도체가 반등하는 날이에요'), '되돌': setv('시가에서 되돌려지는 날이에요'),
    }
    for name, edit in passing.items():
        assert bad(edit) == [], (name, bad(edit))
    assert bad(lambda b: b['evidence'][0].update(ko='외국인 순매도 규모')) != [], 'a day-count-free, number-free evidence line still fails'


def check_copy_rules_all() -> None:
    assert len(ALL_BRIEFS) >= 60, len(ALL_BRIEFS)
    seen_rules = set()
    for label, brief in ALL_BRIEFS:
        problems = cb.copy_problems(brief)
        assert problems == [], (label, problems)
        cb.assert_public_coach_brief(brief)
        seen_rules.add(brief['ruleId'])
        text = json.dumps(brief, ensure_ascii=False)
        assert not cb.NAKED_TRADE.search(text), label
        assert len(brief['verdict']['ko']) <= cb.LIMIT_VERDICT
    assert seen_rules == set(cb.RULE_IDS), sorted(set(cb.RULE_IDS) - seen_rules)
    # Both languages are filled everywhere, the ASCII-only English never carries Hangul.
    for label, brief in ALL_BRIEFS:
        for path, lang, text in cb.strings_of(brief):
            assert (lang == 'ko') or not any('가' <= c <= '힣' for c in text), (label, path, text)


# ---------------------------------------------------------------- 6. a card that cannot be made / never raises

def check_omissions_and_failures() -> None:
    now = at(PRE_NOW)
    good = pre_snap()
    assert cb.build_coach_brief(good, now) is not None
    # No forecast block, a block that is not ok, no current row, no phase: no card.
    assert cb.build_coach_brief(snapshot(None, morning_signals()), now) is None
    for status in ('paused', 'unavailable'):
        assert cb.build_coach_brief(snapshot(morning_block(forecast_row(), 'pre_open', status=status), morning_signals()), now) is None, status
    assert cb.build_coach_brief(snapshot(morning_block(None, 'pre_open'), morning_signals()), now) is None
    assert cb.build_coach_brief(snapshot(morning_block(forecast_row(), 'off'), morning_signals()), now) is None
    # A row without a forecast: the morning sentence cannot be made.
    for row in (forecast_row(state='no_forecast'), dict(forecast_row(), predicted=None), dict(forecast_row(), state='void')):
        assert cb.build_coach_brief(snapshot(morning_block(row, 'pre_open'), morning_signals()), now) is None
    weird = forecast_row()
    weird['predicted']['openCall'] = 'sideways'
    assert cb.build_coach_brief(snapshot(morning_block(weird, 'pre_open'), morning_signals()), now) is None
    # apply_coach_brief writes the block or None, never raises, and leaves the rest of the snapshot alone.
    snap = pre_snap()
    before = deepcopy(snap)
    out = cb.apply_coach_brief(snap, at(PRE_NOW))
    assert out is snap and snap['coachBrief']['ruleId'] == 'C1'
    assert {k: v for k, v in snap.items() if k != 'coachBrief'} == before
    assert cb.apply_coach_brief({}, at(PRE_NOW)) == {'coachBrief': None}
    assert cb.apply_coach_brief({'morningForecast': 5, 'signals': []}, at(PRE_NOW))['coachBrief'] is None
    assert cb.apply_coach_brief(snapshot(morning_block(forecast_row(target='not-a-date'), 'pre_open'), morning_signals()), at(PRE_NOW))['coachBrief'] is None
    broken = {'morningForecast': morning_block(forecast_row(), 'pre_open'), 'signals': {'usd_krw': {'status': 'ok', 'value': 'x'}, 'vix': 7},
              'investorFlows': {'items': ['x', {'date': 5}]}, 'scoreCoreV3': {'dailyDelta': {'us': 'x'}}, 'macroEvents': {'us_cpi': 3},
              'linkSignals': [None, 4]}
    assert cb.apply_coach_brief(broken, at(PRE_NOW))['coachBrief']['ruleId'] == 'C1'   # garbage pieces are dropped, the card stays
    assert cb.apply_coach_brief({'morningForecast': morning_block(forecast_row(), 'pre_open')}, None)['coachBrief'] is None or True  # real clock: no crash
    naive = cb.apply_coach_brief(pre_snap(), datetime(2026, 11, 5, 21, 50))   # a naive time is read as UTC
    assert naive['coachBrief']['generatedAt'] == '2026-11-05T21:50:00Z'
    # A brief that breaks a copy rule is not published: force one through.
    original = cb.pick_verdict
    try:
        cb.pick_verdict = lambda ctx: cb.verdict('C1', 'minus', '미국 급락을 확인해요 밀려', 'x')
        assert cb.apply_coach_brief(pre_snap(), at(PRE_NOW))['coachBrief'] is None
        cb.pick_verdict = lambda ctx: (_ for _ in ()).throw(RuntimeError('boom'))
        assert cb.apply_coach_brief(pre_snap(), at(PRE_NOW))['coachBrief'] is None
    finally:
        cb.pick_verdict = original
    assert cb.apply_coach_brief(pre_snap(), at(PRE_NOW))['coachBrief']['ruleId'] == 'C1'


# ---------------------------------------------------------------- 7. public shape, sanitizer, worker wiring

def check_public_shape_and_wiring() -> None:
    from polarmeter_free_cache_worker import assert_public_payload_safe, sanitize_public_snapshot
    import polarmeter_v3_inputs as inputs
    brief = build(pre_snap(), PRE_NOW, 'public')
    cb.assert_public_coach_brief(brief)
    assert list(brief) == ['schemaVersion', 'generatedAt', 'phase', 'ruleId', 'tone', 'verdict', 'evidence', 'lines', 'next', 'evidenceCount'], list(brief)
    json.dumps(brief, ensure_ascii=False, allow_nan=False)
    # Present -> well formed; absent or null -> passes (old public data, the rollback path, and "no card").
    cb.assert_public_coach_brief_if_present({'coachBrief': brief})
    cb.assert_public_coach_brief_if_present({'coachBrief': None})
    cb.assert_public_coach_brief_if_present({})
    breakers = {
        'schemaVersion': lambda b: b.update(schemaVersion='x'), 'phase': lambda b: b.update(phase='noon'),
        'ruleId': lambda b: b.update(ruleId='C99'), 'tone': lambda b: b.update(tone='red'),
        'verdict': lambda b: b.pop('verdict'), 'verdict ko': lambda b: b['verdict'].update(ko=''),
        'evidence cap': lambda b: b.update(evidence=b['evidence'] * 2), 'evidence key': lambda b: b['evidence'][0].update(key='x'),
        'lines cap': lambda b: b.update(lines=b['lines'] * 2), 'line level': lambda b: b['lines'][0].pop('level'),
        'next cap': lambda b: b.update(next=b['next'] * 2), 'next.at': lambda b: b['next'][0].update(at='2026-11-06T00:00:00+09:00'),
        'next.atKst': lambda b: b['next'][0].update(atKst='9:00'), 'generatedAt': lambda b: b.update(generatedAt='yesterday'),
        'evidenceCount': lambda b: b.update(evidenceCount=0),
    }
    for name, edit in breakers.items():
        broken = deepcopy(brief)
        edit(broken)
        for check in (cb.assert_public_coach_brief, lambda b: cb.assert_public_coach_brief_if_present({'coachBrief': b})):
            try:
                check(broken)
            except (AssertionError, KeyError, TypeError):
                continue
            raise AssertionError(f'a malformed coachBrief passed the public check: {name}')
    # sanitize_public_snapshot passes it through unchanged and writes null when there is none.
    published = sanitize_public_snapshot({'generatedAt': '2026-11-05T21:50:00Z', 'coachBrief': brief})
    assert published['coachBrief'] == brief
    assert sanitize_public_snapshot({'generatedAt': '2026-11-05T21:50:00Z'})['coachBrief'] is None
    assert_public_payload_safe({'coachBrief': brief})
    try:
        assert_public_payload_safe({'coachBrief': {**brief, 'apiKey': 'do-not-publish'}})
    except AssertionError:
        pass
    else:
        raise AssertionError('public payload guard accepted a leaking coachBrief')
    # apply_v3_layers attaches the key after morningForecast and survives every feed being down.
    def down(url: str):
        raise OSError('down')
    out = inputs.apply_v3_layers({}, None, fetch=down, pause=0)
    assert 'morningForecast' in out and 'coachBrief' in out and out['coachBrief'] is None
    order = []
    originals = (inputs.morning_forecast.apply_morning_forecast, inputs.coach_brief.apply_coach_brief)
    try:
        inputs.morning_forecast.apply_morning_forecast = lambda snap, *a, **k: order.append('forecast') or snap
        inputs.coach_brief.apply_coach_brief = lambda snap, now=None: order.append('coach') or snap
        inputs.apply_v3_layers({}, None, fetch=down, pause=0)
    finally:
        inputs.morning_forecast.apply_morning_forecast, inputs.coach_brief.apply_coach_brief = originals
    assert order == ['forecast', 'coach'], order


# ---------------------------------------------------------------- 8. end to end on blocks the real forecast layer writes

def check_end_to_end_with_real_blocks() -> None:
    """Blocks come from `mf.compute_block` (the 1-B contract's fixed world), not from this file's fixtures."""
    first = mfc.run('2026-11-06T06:45:00')
    opened = mfc.run('2026-11-06T10:05:00', first)
    closed = mfc.run('2026-11-06T16:00:00', opened)
    weekend = mfc.run('2026-11-07T10:00:00', closed, us_edit={'SPY': {'2026-11-06': round(mfc.WORLD['us']['SPY']['2026-11-05'] * 1.008, 4)}})
    assert (first['phase'], opened['phase'], closed['phase'], weekend['phase']) == ('pre_open', 'kr_open', 'kr_closed', 'pre_open')
    assert weekend['current']['targetSession'] == '2026-11-09'
    base = lambda block, signals=None: snapshot(block, signals if signals is not None else morning_signals())
    brief = build(base(first), '2026-11-06T06:45:00', 'e2e pre')
    assert brief['ruleId'] == 'C1' and brief['phase'] == 'pre_open', brief
    kospi_open = morning_signals(kospi=signal(5200.0, -3.5, '2026-11-06T00:50:00Z', 5383.8))
    brief = build(base(opened, kospi_open), '2026-11-06T10:05:00', 'e2e kr_open')
    assert brief['ruleId'] == 'C8' and brief['phase'] == 'kr_open', brief['verdict']
    kospi_close = morning_signals(kospi=signal(5059.69, -6.02, '2026-11-06T07:00:00Z', 5383.8))
    brief = build(base(closed, kospi_close), '2026-11-06T16:00:00', 'e2e kr_closed')
    assert brief['ruleId'] == 'C11' and ko(brief) == '오늘 코스피 6.0% 내려 마감했어요', brief['verdict']
    brief = build(base(weekend), '2026-11-07T10:00:00', 'e2e weekend')
    assert brief['ruleId'] == 'C13' and ko(brief) == '금요일 S&P500은 0.8% 올라 끝났어요', brief['verdict']
    assert [i['ko'] for i in brief['next']][0] == '월요일 한국장이 열려요'
    live = deepcopy(first)
    live['phase'] = 'us_open'
    assert build(base(live, morning_signals(sp500=signal(711.0, -1.2, '2026-11-06T14:30:00Z'), soxx=signal(130.0, -2.0, '2026-11-06T14:30:00Z'))),
                 '2026-11-06T23:40:00', 'e2e us_open')['ruleId'] == 'C12'


# ---------------------------------------------------------------- samples (SPEC 1-C: one real output per situation)

def sample_snapshots() -> dict[str, tuple[dict, str]]:
    """The six situations, each as (snapshot, now): the inputs a reader of 'samples' can check by hand."""
    first = mfc.run('2026-11-06T06:45:00')
    opened = mfc.run('2026-11-06T10:05:00', first)
    closed = mfc.run('2026-11-06T16:00:00', opened)
    spy_up = {'SPY': {'2026-11-06': round(mfc.WORLD['us']['SPY']['2026-11-05'] * 1.008, 4)}}
    weekend = mfc.run('2026-11-07T10:00:00', closed, us_edit=spy_up)
    quiet = forecast_row('none', spy=0.2, soxx=-0.3, excess=0.1, top='spyPct')
    quiet['inputs'].update(ewyPct=0.3, kospiPrevPct=0.2)
    kospi = lambda pct, as_of: morning_signals(kospi=signal(5383.8 * (1 + pct / 100), pct, as_of, 5383.8))
    return {
        '1-pre-open-down-sp500': (snapshot(first, morning_signals(), macros=macro('us_cpi', '10월 CPI', '2026-11-10T13:30:00Z')), '2026-11-06T06:50:00'),
        '2-pre-open-no-call': (snapshot(morning_block(quiet, 'pre_open'), morning_signals(usd_krw=signal(1342.0, 0.15, '2026-11-05T21:40:00Z', 1340.0),
                                                                                            vix=signal(19.5, 1.0, '2026-11-05T21:00:00Z', 19.3)),
                                         temps=score((40.0, 41.0), (45.0, 44.0))), '2026-11-06T06:50:00'),
        '3-session-below-open': (snapshot(opened, kospi(-3.5, '2026-11-06T00:50:00Z')), '2026-11-06T10:05:00'),
        '4-session-above-open': (snapshot(opened, kospi(-1.0, '2026-11-06T00:50:00Z')), '2026-11-06T10:05:00'),
        '5-after-close': (snapshot(closed, kospi(-6.02, '2026-11-06T07:00:00Z')), '2026-11-06T16:10:00'),
        '6-weekend': (snapshot(weekend, morning_signals(), flows=flows_ledger('2026-11-06', sells=2)), '2026-11-07T10:00:00'),
    }


def write_samples(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, (snap, now) in sample_snapshots().items():
        moment = at(now)
        brief = cb.build_coach_brief(snap, moment)
        assert brief is not None and cb.copy_problems(brief) == [], name
        out = {'situation': name, 'nowKst': now, 'note': 'Fixed inputs from polarmeter_coach_brief_contract.py (not live data).', 'coachBrief': brief}
        (directory / f'{name}.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sample_brief() -> dict:
    """The 2026-11-06 morning brief, for the security contract."""
    snap, now = sample_snapshots()['1-pre-open-down-sp500']
    return cb.build_coach_brief(snap, at(now))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples-dir', type=Path, default=None)
    args = parser.parse_args()
    check_pre_open_rules()
    check_kr_open_rules()
    check_kr_closed_rules()
    check_us_open_rules()
    check_weekend_rules()
    check_evidence()
    check_baselines()
    check_schedule()
    check_end_to_end_with_real_blocks()
    check_copy_rules_negative()
    check_copy_rules_all()
    check_omissions_and_failures()
    check_public_shape_and_wiring()
    if args.samples_dir:
        write_samples(args.samples_dir)
    print('PASS coach brief: time zones x rules C0-C13, evidence order and floor, baselines L1-L5, schedule (both sides of '
          'daylight saving), copy rules (ko/en), dropped pieces, no card, never raises, public shape (1-C)')


if __name__ == '__main__':
    main()
