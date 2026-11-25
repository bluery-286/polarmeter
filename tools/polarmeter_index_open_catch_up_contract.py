#!/usr/bin/env python3
"""2026-11-24/25: after the Korean open the 1y daily endpoint still ended at the last session, so the app showed
yesterday's KOSPI +6.32% while the market was -4.6%. The 5-minute quote fills in today's price only while its own
regular session (started after the daily quote) is open."""
from __future__ import annotations

import time

import polarmeter_free_provider_probe as probe


def fake(meta):
    return lambda url: {'chart': {'result': [{'meta': meta}]}}


def main() -> int:
    now = int(time.time())
    start = now - 16 * 60                      # the session opened 16 minutes ago
    yesterday = start - 17 * 3600 - 30 * 60    # last session's 15:30 quote
    session = {'regular': {'start': start, 'end': start + 6 * 3600 + 1800}}
    stale = {'regularMarketTime': yesterday, 'regularMarketPrice': 5006.03}
    failures = []
    got = probe.intraday_catch_up('^KS11', stale, fake({'regularMarketPrice': 4753.82, 'regularMarketTime': now - 120,
                                                        'currentTradingPeriod': session}))
    if got != (4753.82, now - 120):
        failures.append(f'open session with a stale daily quote must use the 5-minute quote: {got!r}')
    cases = {
        'fresh daily quote': ({'regularMarketTime': now - 300}, {'regularMarketPrice': 1, 'regularMarketTime': now,
                                                                 'currentTradingPeriod': session}),
        'market closed (5-minute quote is the same session)': (stale, {
            'regularMarketPrice': 5006.03, 'regularMarketTime': yesterday,
            'currentTradingPeriod': {'regular': {'start': yesterday - 6 * 3600, 'end': yesterday}}}),
        'before the open': (stale, {'regularMarketPrice': 5010, 'regularMarketTime': now - 60,
                                    'currentTradingPeriod': {'regular': {'start': now + 600, 'end': now + 7 * 3600}}}),
        'no trading period': (stale, {'regularMarketPrice': 4753.82, 'regularMarketTime': now - 120}),
    }
    for name, (daily, fresh) in cases.items():
        got = probe.intraday_catch_up('^KS11', daily, fake(fresh))
        if got is not None:
            failures.append(f'{name}: expected no catch-up, got {got!r}')
    def broken(url):
        raise OSError('network down')
    if probe.intraday_catch_up('^KS11', stale, broken) is not None:
        failures.append('a network error must leave the daily result')
    if failures:
        print('\n'.join(failures))
        return 1
    print('index open catch-up contract ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
