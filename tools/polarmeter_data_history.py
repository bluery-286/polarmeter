#!/usr/bin/env python3
"""Keep PolarMeter's own record on the `data-history` branch (plan S3, 2026-11-10).

gh-pages is force-pushed every run, so yesterday's scores, the forecast ledgers and the news cards
were nowhere once a newer snapshot replaced them. This module appends them to plain files that the
Pages workflow commits to the orphan branch `data-history` (append-only, never force-pushed):

  scores/YYYY-MM.jsonl          one row per KST day and slot (kr_close, us_close): v1 and v3 scores,
                                components, the input quotes and the data-quality state
  news/YYYY-MM-DD-<slot>.json   the 30 public cards at that slot (title, source, tone, cluster, link)
  forecast/ledger.jsonl         every live morning-forecast and evening-brief row ever published,
                                one line per (kind, targetSession); the issued row keeps its inputs
  state/<key>.json              the last published block of each ledger the worker carries forward
                                (morningForecast, eveningBrief, temperatureHistory, investorFlows),
                                rewritten only when its rows change

restore() puts a state block back when the previous public snapshot could not be read, so a missed
fetch never makes the forecast restart from a recomputed backfill.

CLI:  python3 tools/polarmeter_data_history.py archive --snapshot <public snapshot json> --history <dir>
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

KST = timezone(timedelta(hours=9))
STATE_KEYS = ('morningForecast', 'eveningBrief', 'temperatureHistory', 'investorFlows')
VOLATILE_KEYS = {'generatedAt', 'updatedAt', 'nextIssueAt', 'phase'}
NEWS_FIELDS = ('headline', 'originalHeadline', 'displayHeadline', 'sourceName', 'publishedAt', 'url', 'impactTarget',
               'impactTone', 'category', 'critical', 'criticalReason', 'marketImpactScore', 'qualityScore',
               'issueClusterKey', 'relatedReportCount', 'region', 'language')
SIGNAL_FIELDS = ('value', 'changePct', 'previousClose', 'dataAsOf', 'status', 'provider', 'changeSource')


def parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def slot_for(generated_at: datetime) -> str | None:
    """Which daily record a run fills, from its KST time. None: not a recording run.

    us_close: 06:00-11:59 KST, Tuesday-Saturday (the US session that ended overnight).
    kr_close: 16:00-23:59 KST, Monday-Friday (after the 15:30 close and its 15:40 scoring).
    The first run inside a slot writes it; later runs leave it alone. Holidays still get a row
    (its sessionDate fields say which session the numbers belong to).
    """
    local = generated_at.astimezone(KST)
    if 6 <= local.hour < 12 and local.weekday() in (1, 2, 3, 4, 5):
        return 'us_close'
    if local.hour >= 16 and local.weekday() < 5:
        return 'kr_close'
    return None


def stable(value: Any) -> Any:
    """A block without the fields that change on every run, for change detection."""
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_text(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding='utf-8') == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)
    return True


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def score_row(snapshot: Mapping[str, Any], date_kst: str, slot: str) -> dict[str, Any]:
    v1 = snapshot.get('scoreCore') or {}
    v3 = snapshot.get('scoreCoreV3') or {}
    quality = snapshot.get('dataQuality') or {}

    def v1_market(market: str) -> Any:
        block = v1.get(market)
        return deepcopy(block) if isinstance(block, dict) else None

    def v3_market(market: str) -> Any:
        block = v3.get(market)
        if not isinstance(block, dict):
            return None
        return {key: deepcopy(block.get(key)) for key in ('sessionDate', 'score', 'rawScore', 'label', 'coverage',
                                                           'components', 'weights', 'guardrail')}

    signals = {key: {field: signal.get(field) for field in SIGNAL_FIELDS if field in signal}
               for key, signal in sorted((snapshot.get('signals') or {}).items()) if isinstance(signal, dict)}
    return {
        'dateKst': date_kst, 'slot': slot, 'generatedAt': snapshot.get('generatedAt'),
        'status': snapshot.get('status'), 'displayMode': quality.get('displayMode'),
        'v1': {'formulaVersion': v1.get('formulaVersion'), 'us': v1_market('us'), 'kr': v1_market('kr')},
        'v3': {'formulaVersion': v3.get('formulaVersion'), 'status': v3.get('status'),
               'us': v3_market('us'), 'kr': v3_market('kr'), 'missingInputs': deepcopy(v3.get('missingInputs'))},
        'signals': signals,
    }


def forecast_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for kind, key in (('morning', 'morningForecast'), ('evening', 'eveningBrief')):
        block = snapshot.get(key)
        if not isinstance(block, dict):
            continue
        model = block.get('modelVersion')
        candidates = list(block.get('ledger') or [])
        if isinstance(block.get('current'), dict):
            candidates.append(block['current'])  # last: the current row carries inputs the ledger lines drop
        for row in candidates:
            if not isinstance(row, dict) or row.get('origin') != 'live' or not isinstance(row.get('targetSession'), str):
                continue
            line = {k: deepcopy(v) for k, v in row.items() if k != 'copy'}
            line['kind'] = kind
            line.setdefault('modelVersion', model)
            rows.append(line)
    return rows


def merge_ledger(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing:
        if isinstance(row.get('kind'), str) and isinstance(row.get('targetSession'), str):
            by_key[(row['kind'], row['targetSession'])] = row
    for row in incoming:
        key = (row['kind'], row['targetSession'])
        merged = dict(by_key.get(key) or {})
        kept_inputs = merged.get('inputs')
        merged.update(row)
        if row.get('inputs') is None and kept_inputs is not None:
            merged['inputs'] = kept_inputs  # a ledger line without inputs never erases the issued inputs
        by_key[key] = merged
    return [by_key[key] for key in sorted(by_key)]


def archive(snapshot: Mapping[str, Any], history: Path) -> list[str]:
    """Write this snapshot's share of the record into `history`. Returns the changed paths (relative)."""
    generated = parse_time(snapshot.get('generatedAt'))
    if generated is None:
        raise ValueError('snapshot has no generatedAt')
    changed: list[str] = []
    date_kst = generated.astimezone(KST).date().isoformat()
    slot = slot_for(generated)

    if slot:
        scores = history / f'scores/{date_kst[:7]}.jsonl'
        rows = read_jsonl(scores)
        if not any(row.get('dateKst') == date_kst and row.get('slot') == slot for row in rows):
            rows.append(score_row(snapshot, date_kst, slot))
            rows.sort(key=lambda row: (str(row.get('dateKst')), str(row.get('slot'))))
            if write_text(scores, ''.join(dumps(row) + '\n' for row in rows)):
                changed.append(str(scores.relative_to(history)))
        news_path = history / f'news/{date_kst}-{slot}.json'
        items = ((snapshot.get('news') or {}).get('items')) or []
        if items and not news_path.exists():
            cards = [{field: item.get(field) for field in NEWS_FIELDS if field in item} for item in items if isinstance(item, dict)]
            body = json.dumps({'generatedAt': snapshot.get('generatedAt'), 'slot': slot, 'items': cards},
                              ensure_ascii=False, indent=1) + '\n'
            if write_text(news_path, body):
                changed.append(str(news_path.relative_to(history)))

    ledger_path = history / 'forecast/ledger.jsonl'
    merged = merge_ledger(read_jsonl(ledger_path), forecast_rows(snapshot))
    if merged and write_text(ledger_path, ''.join(dumps(row) + '\n' for row in merged)):
        changed.append(str(ledger_path.relative_to(history)))

    for key in STATE_KEYS:
        block = snapshot.get(key)
        if not isinstance(block, dict):
            continue
        path = history / f'state/{key}.json'
        before = read_json(path)
        if isinstance(before, dict) and stable(before) == stable(block):
            continue
        if write_text(path, json.dumps(block, ensure_ascii=False, indent=1, sort_keys=True) + '\n'):
            changed.append(str(path.relative_to(history)))
    return changed


def restore(previous_public: Mapping[str, Any] | None, history: Path | None) -> tuple[dict[str, Any], list[str]]:
    """Fill ledger blocks missing from the previous public snapshot with the archived state.

    Only absent blocks are filled: a block that is present always wins, even when it looks older.
    """
    previous = dict(previous_public or {})
    restored: list[str] = []
    if history is None or not history.is_dir():
        return previous, restored
    for key in STATE_KEYS:
        if isinstance(previous.get(key), dict) and previous[key]:
            continue
        block = read_json(history / f'state/{key}.json')
        if isinstance(block, dict) and block:
            previous[key] = block
            restored.append(key)
    return previous, restored


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('archive')
    run.add_argument('--snapshot', type=Path, required=True)
    run.add_argument('--history', type=Path, required=True)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
    changed = archive(snapshot, args.history)
    print(json.dumps({'dataHistoryChanged': changed}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
