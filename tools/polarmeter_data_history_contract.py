#!/usr/bin/env python3
"""Contract: the data-history record is append-only, one row per day and slot, keeps issued inputs,
rewrites ledger state only when rows change, and restores only what the previous snapshot lacks.

Run: PYTHONPATH=tools python3 tools/polarmeter_data_history_contract.py
"""
from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import polarmeter_data_history as history


def snapshot(generated: str, *, ledger_state: str = 'issued', with_inputs: bool = True) -> dict:
    current = {'targetSession': '2026-11-10', 'basisKrSession': '2026-11-09', 'origin': 'live', 'state': ledger_state,
               'predicted': {'openPct': 0.8}, 'copy': {'title': 'x'}}
    if with_inputs:
        current['inputs'] = {'spyPct': 2.37, 'soxxPct': 4.1, 'ewyPct': 1.2}
    return {
        'generatedAt': generated, 'status': 'partial', 'dataQuality': {'displayMode': 'limited'},
        'scoreCore': {'formulaVersion': 'v1', 'us': {'score': 72}, 'kr': {'score': 56}},
        'scoreCoreV3': {'formulaVersion': 'v3', 'status': 'ok', 'us': {'sessionDate': '2026-11-09', 'score': 64, 'label': '온기',
                                                                     'components': {'trend': 77.2}},
                        'kr': {'sessionDate': '2026-11-09', 'score': 37}},
        'signals': {'sp500': {'value': 6900.1, 'changePct': 2.37, 'dataAsOf': '2026-11-09T21:00:00Z', 'status': 'ok',
                              'reliability': {'secret': 'not copied'}}},
        'news': {'items': [{'headline': '나스닥 2.74% 급등', 'sourceName': 'A', 'impactTone': 'negative',
                            'url': 'https://example.invalid/a', 'whyImportant': 'not copied'}]},
        'morningForecast': {'schemaVersion': 's', 'modelVersion': 'm1', 'generatedAt': generated, 'phase': 'issued',
                            'current': current,
                            'ledger': [{'targetSession': '2026-11-09', 'basisKrSession': '2026-11-06', 'origin': 'live',
                                        'state': 'scored'},
                                       {'targetSession': '2026-11-06', 'basisKrSession': '2026-11-05', 'origin': 'backfill',
                                        'state': 'scored'}],
                            'scorecard': {'live': {'carried': {'calls': 1}}}},
        'temperatureHistory': {'updatedAt': generated, 'items': [{'dateKst': '2026-11-09', 'us': 72}]},
    }


# Slots: Tuesday 08:05 KST -> us_close; Monday 16:10 KST -> kr_close; Monday 08:05 (no US session overnight) and 13:00 -> none.
assert history.slot_for(datetime(2026, 11, 9, 23, 5, tzinfo=timezone.utc)) == 'us_close'
assert history.slot_for(datetime(2026, 11, 9, 7, 10, tzinfo=timezone.utc)) == 'kr_close'
assert history.slot_for(datetime(2026, 11, 8, 23, 5, tzinfo=timezone.utc)) is None
assert history.slot_for(datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)) is None

with tempfile.TemporaryDirectory(prefix='polarmeter-history-contract-',
                                 dir=Path(__file__).resolve().parents[1]) as folder:
    root = Path(folder) / 'history'
    first = history.archive(snapshot('2026-11-09T23:05:00Z'), root)
    assert sorted(first) == ['forecast/ledger.jsonl', 'news/2026-11-10-us_close.json', 'scores/2026-11.jsonl',
                             'state/morningForecast.json', 'state/temperatureHistory.json'], first
    score = json.loads((root / 'scores/2026-11.jsonl').read_text(encoding='utf-8'))
    assert score['dateKst'] == '2026-11-10' and score['slot'] == 'us_close'
    assert score['v1']['kr']['score'] == 56 and score['v3']['kr']['score'] == 37
    assert score['signals']['sp500'] == {'value': 6900.1, 'changePct': 2.37, 'dataAsOf': '2026-11-09T21:00:00Z', 'status': 'ok'}
    news = json.loads((root / 'news/2026-11-10-us_close.json').read_text(encoding='utf-8'))
    assert news['items'][0]['impactTone'] == 'negative' and 'whyImportant' not in news['items'][0]

    ledger = [json.loads(line) for line in (root / 'forecast/ledger.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [(r['kind'], r['targetSession']) for r in ledger] == [('morning', '2026-11-09'), ('morning', '2026-11-10')], \
        'live rows only, sorted, backfill excluded'
    assert ledger[1]['inputs']['spyPct'] == 2.37 and 'copy' not in ledger[1] and ledger[1]['modelVersion'] == 'm1'

    # A later run in the same slot: no new score or news file; same rows -> nothing changes at all.
    same = snapshot('2026-11-09T23:35:00Z')
    assert history.archive(same, root) == [], 'volatile fields alone never rewrite the record'
    assert len((root / 'scores/2026-11.jsonl').read_text(encoding='utf-8').splitlines()) == 1

    # 09:05 scoring: the row moves on and loses inputs in the ledger line -> the issued inputs stay.
    scored = snapshot('2026-11-10T00:10:00Z', ledger_state='open_scored', with_inputs=False)
    scored['morningForecast']['ledger'].append(deepcopy(scored['morningForecast']['current']))
    scored['morningForecast']['current'] = None
    changed = history.archive(scored, root)
    assert changed == ['forecast/ledger.jsonl', 'state/morningForecast.json'], changed
    ledger = [json.loads(line) for line in (root / 'forecast/ledger.jsonl').read_text(encoding='utf-8').splitlines()]
    assert ledger[1]['state'] == 'open_scored' and ledger[1]['inputs']['spyPct'] == 2.37

    # Kr close the same day: a second score row.
    history.archive(snapshot('2026-11-10T07:10:00Z', ledger_state='open_scored'), root)
    rows = [json.loads(line) for line in (root / 'scores/2026-11.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [(r['dateKst'], r['slot']) for r in rows] == [('2026-11-10', 'kr_close'), ('2026-11-10', 'us_close')]

    # Restore: only missing blocks, and only from a real folder.
    filled, restored = history.restore({'generatedAt': 'x'}, root)
    assert restored == ['morningForecast', 'temperatureHistory'] and filled['morningForecast']['modelVersion'] == 'm1'
    present = {'morningForecast': {'ledger': [], 'schemaVersion': 'newer'}}
    kept, restored = history.restore(present, root)
    assert kept['morningForecast']['schemaVersion'] == 'newer' and restored == ['temperatureHistory']
    assert history.restore({}, None) == ({}, [])
    assert history.restore({}, root / 'missing') == ({}, [])

print('polarmeter data history contract passed')
