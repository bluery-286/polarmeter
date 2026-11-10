#!/usr/bin/env python3
"""Contract (plan S7): the morning forecast says only what its record supports.

1. The scorecard splits the recent window by origin; `recent` keeps its old shape for app 1.0.22.
2. No public string counts a recomputed (backfill) day as a recent forecast.
3. Strong titles hedge ("~할 것으로 보여요" / "looks set to"); no title ends in a flat assertion.
4. The open range uses the 80th percentile error, and its note states how often that range held.
5. A past line on fewer than 100 days is a rough share; the close is information, not a scored call.

Run: PYTHONPATH=tools python3 tools/polarmeter_forecast_honesty_contract.py
"""
from __future__ import annotations

import json
import re
from copy import deepcopy

import polarmeter_morning_forecast as mf
import polarmeter_morning_forecast_contract as base
import polarmeter_push_outbox as push

# 1. recentLive / recentBacktest beside the unchanged recent block.
previous = base.make_rows(25, 8, 15, live_from=20)
block = base.run('2026-09-01T07:00:00', previous)
score = block['scorecard']
assert set(score['recent']) == {'sessions', 'calls', 'openHits', 'closeCalls', 'closeHits', 'liveSessions', 'marks'}, score['recent']
live, back = score['recentLive'], score['recentBacktest']
assert live['sessions'] + back['sessions'] == score['recent']['sessions']
assert live['sessions'] == score['recent']['liveSessions']
assert live['calls'] + back['calls'] == score['recent']['calls'] and live['openHits'] + back['openHits'] == score['recent']['openHits']
window = [r for r in block['ledger'] if r['state'] in ('open_scored', 'closed_scored', 'no_forecast')][-20:]
assert live['marks'] == [mf.row_mark(r) for r in window if r['origin'] == 'live'], live

# 2. A mixed window never reads as "recent" in the push, and the public block never says so either.
assert push.record_clause(score) is None or '최근' not in push.record_clause(score)[0], push.record_clause(score)
raw = json.dumps(block, ensure_ascii=False)
assert '최근' not in raw.replace('최근 1년', ''), 'no public string calls a mixed window "recent"'

# 3. Hedged titles.
ASSERTIVE_KO = re.compile(r'시작해요$')
ASSERTIVE_EN = re.compile(r'\bis set to\b')
today = base.date(2026, 11, 6)
for call in ('down', 'up'):
    for strength in ('moderate', 'strong'):
        title = mf.title_copy(base.synthetic_row(call, strength), today)
        assert not ASSERTIVE_KO.search(title['ko']) and not ASSERTIVE_EN.search(title['en']), title
        assert title['ko'].endswith(('것으로 보여요', '가능성이 높아요')), title

# 4. The range: p80 when present, with an honest note.
model = mf.load_model()
for band in model['openBands']:
    assert band['p80AbsErr'] >= band['p50AbsErr'], band
    assert 0 <= band['p80RecentCoverage'] <= 1 and band['recentDays'] >= 20, band
strong = base.synthetic_row('down', 'strong')
strong['predicted']['openBand'] = mf.open_band(-3.0, 'strong', model)
assert mf.open_band(-3.0, 'strong', model) == [round(-3.0 - model['openBands'][2]['p80AbsErr'], 2),
                                              round(-3.0 + model['openBands'][2]['p80AbsErr'], 2)]
note = mf.range_note_copy(strong['predicted'], model)
tens = int(model['openBands'][2]['p80RecentCoverage'] * 10 + 0.5)
assert note == {'ko': f'최근 1년 10번 중 {tens}번은 이 범위 안', 'en': f'{tens} of 10 opens fell inside in the last year'}, note
no_recent = deepcopy(model)
for band in no_recent['openBands']:
    band.pop('p80RecentCoverage')
assert mf.range_note_copy(strong['predicted'], no_recent)['ko'] == '과거 10번 중 8번은 이 범위 안'
old_model = deepcopy(model)
for band in old_model['openBands']:
    band.pop('p80AbsErr')
assert mf.open_band(-3.0, 'strong', old_model) == [round(-3.0 - model['openBands'][2]['p50AbsErr'], 2),
                                                  round(-3.0 + model['openBands'][2]['p50AbsErr'], 2)], 'old model files still work'
assert mf.range_note_copy(strong['predicted'], old_model) is None, 'no note without a p80 range'
assert mf.range_note_copy(base.synthetic_row('down', 'strong')['predicted'], model) is None, 'a p50-era frozen range gets no note'

# 5. Rough share below 100 days, exact counts at 100+; the close is info only.
row = base.synthetic_row('down', 'strong')
row['analog'] = {'days': 57, 'openSame': 54, 'closeSame': 50, 'period': '2024-11-06 ~ 2026-11-06', 'widened': False}
past = mf.past_copy(row)
assert '열에 아홉쯤' in past['ko'] and '(57일, 2024년 11월부터)' in past['ko'] and '54' not in past['ko'], past
row['analog'].update(days=120, openSame=100)
assert '120일 중 100일' in mf.past_copy(row)['ko']
close = mf.close_copy(row, model)
assert close['closeStat']['ko'] == '120일 중 50일' and '마이너스 마감' in close['closeNote']['ko'], close
assert '쪽' not in close['closeStat']['ko'], 'no "하락 쪽" call'
done = base.synthetic_row('down', 'strong', 'closed_scored', open_pct=-2.2, close_pct=-3.0, intraday=-0.8, in_band=True)
rows = mf.result_rows_copy(done)
assert rows[2]['key'] == 'close' and rows[2]['hit'] is None and done['result']['closeHit'] is True, rows[2]

print('polarmeter forecast honesty contract passed')
