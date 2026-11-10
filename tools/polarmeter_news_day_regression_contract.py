#!/usr/bin/env python3
"""News-day regression: replay a whole stored day of raw RSS headlines and check the selected 30 cards.

Each folder tools/fixtures/news-days/<day>/ holds
  feed.json    raw feedResults saved by the live probe (title, source, time, link; no article bodies)
  expect.json  hand labels (tone per headline, same-event groups) and the day's index moves

Checks per day
  labels      a labelled headline, when selected, has one of its allowed tones
  same_event  cards of one labelled event share one tone, and at most maxSelected are selected
  rally_tone  when 2+ indexes of one market moved over +1.5% (or under -1.5%), the opposite-tone
              share of all cards is at most maxNegativeShareOnRally
  caps        at most maxPerCategory cards per category and maxPerSource per source
  kr_market   on a weekday at least minKrMarket cards about the domestic market (impactTarget kr)
  replay      two replays give identical cards (the clock is pinned to the capture time)

Run: PYTHONPATH=tools python3 tools/polarmeter_news_day_regression_contract.py [--report-only] [--day 2026-11-10]
Without --report-only a failed check exits 1. Plan S2: this starts red on 2026-11-10 (tone inverted,
one US close in 5 cards, 1 domestic market card); S4/S5 made it green and it runs in the Pages workflow.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import polarmeter_news_rss_probe as probe

FIXTURES = Path(__file__).resolve().parent / 'fixtures/news-days'
OPPOSITE = {'up': 'negative', 'down': 'positive'}


def card_text(item: dict) -> str:
    return ' '.join(str(item.get(field) or '') for field in ('headline', 'originalHeadline', 'displayHeadline'))


def matching(items: list[dict], needle: str) -> list[dict]:
    return [item for item in items if needle in card_text(item)]


def check_day(folder: Path) -> tuple[list[str], dict]:
    feed = json.loads((folder / 'feed.json').read_text(encoding='utf-8'))
    expect = json.loads((folder / 'expect.json').read_text(encoding='utf-8'))
    report = probe.replay_report(feed)
    again = probe.replay_report(feed)
    items = report['items']
    failures: list[str] = []
    if json.dumps(items, sort_keys=True, ensure_ascii=False) != json.dumps(again['items'], sort_keys=True, ensure_ascii=False):
        failures.append('replay: two replays of the same feed differ')

    for label in expect.get('labels', []):
        for item in matching(items, label['match']):
            if item.get('impactTone') not in label['tone']:
                failures.append(f"labels: '{label['match']}' is {item.get('impactTone')}, expected {'/'.join(label['tone'])}")

    for group in expect.get('sameEvent', []):
        selected = [item for needle in group['match'] for item in matching(items, needle)]
        unique = {id(item): item for item in selected}.values()
        tones = sorted({item.get('impactTone') for item in unique})
        if len(tones) > 1:
            failures.append(f"same_event: {group['name']} cards disagree on tone {tones}")
        if len(unique) > group.get('maxSelected', 2):
            failures.append(f"same_event: {group['name']} has {len(unique)} cards selected, max {group.get('maxSelected', 2)}")

    caps = expect.get('caps', {})
    tones = collections.Counter(item.get('impactTone') for item in items)
    for market, moves in (expect.get('marketMoves') or {}).items():
        up = sum(1 for value in moves.values() if value > 1.5)
        down = sum(1 for value in moves.values() if value < -1.5)
        direction = 'up' if up >= 2 else 'down' if down >= 2 else None
        if direction and items:
            share = tones[OPPOSITE[direction]] / len(items)
            if share > caps.get('maxNegativeShareOnRally', 0.5):
                failures.append(f"rally_tone: {market} moved {direction} but {OPPOSITE[direction]} cards are "
                                f"{tones[OPPOSITE[direction]]}/{len(items)} ({share:.0%})")

    for field, limit, name in (('category', caps.get('maxPerCategory', 8), 'category'),
                               ('sourceName', caps.get('maxPerSource', 3), 'source')):
        for value, count in collections.Counter(item.get(field) for item in items).items():
            if count > limit:
                failures.append(f'caps: {name} {value} has {count} cards, max {limit}')

    if expect.get('weekday'):
        kr = sum(1 for item in items if item.get('impactTarget') == 'kr')
        if kr < caps.get('minKrMarket', 2):
            failures.append(f"kr_market: {kr} domestic market card(s), min {caps.get('minKrMarket', 2)}")

    summary = {'day': expect.get('day'), 'items': len(items), 'tones': dict(tones), 'failures': len(failures)}
    return failures, summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--report-only', action='store_true', help='print failures but exit 0 (until plan S4/S5 land)')
    parser.add_argument('--day', help='check one fixture day only')
    args = parser.parse_args()
    days = sorted(path for path in FIXTURES.iterdir() if path.is_dir() and (path / 'feed.json').exists())
    if args.day:
        days = [path for path in days if path.name == args.day]
    if not days:
        print('news-day regression: no fixture days found')
        return 1
    total = 0
    for folder in days:
        failures, summary = check_day(folder)
        total += len(failures)
        print(json.dumps(summary, ensure_ascii=False))
        for failure in failures:
            print(f'  FAIL {failure}')
    if total:
        print(f'news-day regression: {total} failure(s){" (report only)" if args.report_only else ""}')
        return 0 if args.report_only else 1
    print('news-day regression: PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
