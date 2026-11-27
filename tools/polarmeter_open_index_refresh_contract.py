#!/usr/bin/env python3
"""2026-11-25: a build right after the Korean open kept the last session's KOSPI and a 60-minute TTL, so nothing
rebuilt for 20+ minutes. While a market is open and its core index is 45+ minutes old, the TTL is 10 minutes."""
from __future__ import annotations

import polarmeter_free_cache_worker as worker


def snap(generated, kospi_as_of, kosdaq_as_of=None):
    return {'generatedAt': generated, 'signals': {
        'kospi': {'dataAsOf': kospi_as_of}, 'kosdaq': {'dataAsOf': kosdaq_as_of or kospi_as_of},
        'sp500': {'dataAsOf': '2026-11-24T21:00:00Z'}, 'nasdaq100': {'dataAsOf': '2026-11-24T21:00:00Z'}}}


def main() -> int:
    failures = []
    base = worker.recommended_market_ttl_minutes()
    cases = [
        ('KR open, KOSPI still yesterday 20:05', snap('2026-11-25T00:16:02Z', '2026-11-24T11:05:40Z'), 10),
        ('KR open, KOSPI 20 minutes old', snap('2026-11-25T00:37:34Z', '2026-11-25T00:16:50Z'), base),
        ('KR open, KOSDAQ missing', snap('2026-11-25T00:37:34Z', '2026-11-25T00:16:50Z', '') | {}, base),
        ('KR closed evening, US closed', snap('2026-11-25T12:00:00Z', '2026-11-25T06:35:00Z'), base),
    ]
    missing = snap('2026-11-25T00:37:34Z', '2026-11-25T00:16:50Z')
    missing['signals']['kosdaq'] = {}
    cases[2] = ('KR open, KOSDAQ missing a time', missing, 10)
    # 2026-11-27: KOSDAQ +4.08% at the build, +5.68% 44 minutes later. A big-move day builds every 15 minutes.
    big = snap('2026-11-27T01:16:05Z', '2026-11-27T01:10:00Z')
    big['signals']['kosdaq']['changePct'] = 4.08
    calm = snap('2026-11-27T01:16:05Z', '2026-11-27T01:10:00Z')
    calm['signals']['kospi']['changePct'] = -1.2
    shut = snap('2026-11-27T12:00:00Z', '2026-11-27T06:35:00Z')
    shut['signals']['kospi']['changePct'] = -4.6
    drop = snap('2026-11-27T01:16:05Z', '2026-11-27T01:10:00Z')
    drop['signals']['kospi']['changePct'] = -2.0
    cases += [('KR open, KOSDAQ +4.08%', big, 15), ('KR open, KOSPI -1.2%', calm, base),
              ('KR closed, KOSPI -4.6%', shut, base), ('KR open, KOSPI -2.0%', drop, 15)]
    for name, snapshot, want in cases:
        got = worker.public_market_ttl_minutes(snapshot)
        if got != min(want, base):
            failures.append(f'{name}: ttl {got} != {min(want, base)}')
    if failures:
        print('\n'.join(failures))
        return 1
    print('open index refresh contract ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
