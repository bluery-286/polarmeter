#!/usr/bin/env python3
"""Contract (plan S6): 24-hour instruments use a fixed previous reference that does not move with the clock
inside a Korean day, and the basis is published.

Run: PYTHONPATH=tools python3 tools/polarmeter_change_basis_contract.py
"""
from __future__ import annotations

from datetime import datetime, timezone

import polarmeter_free_provider_probe as probe


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def kst(moment: datetime) -> str:
    return moment.astimezone(probe.SEOUL).strftime('%Y-%m-%d %H:%M')


# 1. KRW fallback basis: 15:30 KST of the last Seoul trading day before today, the same all day long.
#    Tuesday 2026-11-10: 00:30 UTC (09:30 KST, the old reset point) and 08:00 UTC (17:00 KST) agree.
for now in ('2026-11-09T23:05:00Z', '2026-11-10T00:30:00Z', '2026-11-10T08:00:00Z', '2026-11-10T14:59:00Z'):
    assert kst(probe.fixed_basis_time('seoul_1530', utc(now))) == '2026-11-09 15:30', now
# Monday morning: Friday. After a KRX holiday (2026-12-25 Fri): Thursday.
assert kst(probe.fixed_basis_time('seoul_1530', utc('2026-11-09T00:30:00Z'))) == '2026-11-06 15:30'
assert kst(probe.fixed_basis_time('seoul_1530', utc('2026-12-28T01:00:00Z'))) == '2026-12-24 15:30'

# 2. DXY/WTI basis: 17:00 New York of the weekday before the US session that is open or closed last.
#    It holds through the whole Korean day (old reset: 05:30 UTC = 14:30 KST) and moves at 09:30 ET.
for now in ('2026-11-09T23:05:00Z', '2026-11-10T05:30:00Z', '2026-11-10T13:00:00Z', '2026-11-10T14:29:00Z'):
    assert kst(probe.fixed_basis_time('ny_1700', utc(now))) == '2026-11-07 07:00', now  # Fri 17:00 ET
assert kst(probe.fixed_basis_time('ny_1700', utc('2026-11-10T14:31:00Z'))) == '2026-11-10 07:00'  # Tue 09:31 ET -> Mon 17:00
# Monday 10:00 KST (Sunday ET): the last session was Friday -> Thursday 17:00 ET.
assert kst(probe.fixed_basis_time('ny_1700', utc('2026-11-09T01:00:00Z'))) == '2026-11-06 07:00'


# 3. value_at: the bar that ends at the moment; never a later bar; a skipped bar falls back within an hour.
def chart(points: list[tuple[str, float | None]]) -> dict:
    return {'chart': {'result': [{'timestamp': [int(utc(t).timestamp()) for t, _ in points],
                                  'indicators': {'quote': [{'close': [c for _, c in points]}]}}]}}


moment = utc('2026-11-09T06:30:00Z')  # 15:30 KST
result = chart([('2026-11-09T05:30:00Z', 1350.0), ('2026-11-09T06:00:00Z', 1347.28), ('2026-11-09T06:30:00Z', 1360.0)])
assert probe.value_at(result['chart']['result'][0], moment) == 1347.28
gap = chart([('2026-11-09T05:30:00Z', 1349.0), ('2026-11-09T06:00:00Z', None), ('2026-11-09T06:30:00Z', 1360.0)])
assert probe.value_at(gap['chart']['result'][0], moment) == 1349.0, 'skipped bar: the last earlier close'
assert probe.value_at(chart([('2026-11-09T06:30:00Z', 1360.0)])['chart']['result'][0], moment) is None

# 4. USD/KRW: Naver's previous-day notice first, Yahoo's 15:30 bar second, nothing implausible.
naver = {'result': [{'localTradedAt': '2026-11-10', 'closePrice': '1,344.70'},
                    {'localTradedAt': '2026-11-09', 'closePrice': '1,342.00'}]}


def fake(rows: dict | None, bars: dict):
    def fetch(url: str) -> dict:
        if 'm.stock.naver.com' in url:
            if rows is None:
                raise TimeoutError('naver down')
            return rows
        return bars
    return fetch


now = utc('2026-11-10T01:00:00Z')
assert probe.fixed_basis_change('KRW=X', 1344.48, now, fake(naver, result))[:2] == (1342.0, 'seoul_notice')
assert probe.fixed_basis_change('KRW=X', 1344.48, now, fake(None, result))[:2] == (1347.28, 'seoul_1530')
far = {'result': [{'localTradedAt': '2026-11-09', 'closePrice': '1,500.00'}]}
assert probe.fixed_basis_change('KRW=X', 1344.48, now, fake(far, result))[1] == 'seoul_1530', '3%+ off -> not a base'
assert probe.fixed_basis_change('KRW=X', None, now, fake(naver, result)) is None
assert probe.fixed_basis_change('SPY', 600.0, now, fake(naver, result)) is None, 'stocks keep regular_close'
assert probe.FIXED_CHANGE_BASIS == {'KRW=X': 'seoul_1530', 'DX-Y.NYB': 'ny_1700', 'CL=F': 'ny_1700'}

print('polarmeter change basis contract passed')
