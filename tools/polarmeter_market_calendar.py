#!/usr/bin/env python3
"""Market-session calendar helpers for PolarMeter operations.

This is intentionally small and deterministic. It covers the US equity market
holiday rules used by NYSE/Nasdaq and the KR exchange holidays currently needed
by the PolarMeter beta ops checks.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST_ZONE = ZoneInfo('Asia/Seoul')
NY_ZONE = ZoneInfo('America/New_York')


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def observed_fixed_holiday(day: date, month: int, day_of_month: int) -> bool:
    fixed = date(day.year, month, day_of_month)
    observed = fixed
    if fixed.weekday() == 5:
        observed = fixed - timedelta(days=1)
    elif fixed.weekday() == 6:
        observed = fixed + timedelta(days=1)
    return day == fixed or day == observed


def easter_date(year: int) -> date:
    # Gregorian computus.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_market_closed_reason(now: datetime) -> str | None:
    day = now.astimezone(NY_ZONE).date()
    year = day.year
    if day.weekday() >= 5:
        return '미국장 주말 휴장'
    if observed_fixed_holiday(day, 1, 1):
        return "미국장 휴장(New Year's Day)"
    if day == nth_weekday(year, 1, 0, 3):
        return '미국장 휴장(MLK Day)'
    if day == nth_weekday(year, 2, 0, 3):
        return "미국장 휴장(Presidents' Day)"
    if day == easter_date(year) - timedelta(days=2):
        return '미국장 휴장(Good Friday)'
    if day == last_weekday(year, 5, 0):
        return '미국장 휴장(Memorial Day)'
    if observed_fixed_holiday(day, 6, 19):
        return '미국장 휴장(Juneteenth)'
    if observed_fixed_holiday(day, 7, 4):
        return '미국장 휴장(Independence Day)'
    if day == nth_weekday(year, 9, 0, 1):
        return '미국장 휴장(Labor Day)'
    if day == nth_weekday(year, 11, 3, 4):
        return '미국장 휴장(Thanksgiving)'
    if observed_fixed_holiday(day, 12, 25):
        return '미국장 휴장(Christmas)'
    return None


def kr_market_closed_reason(now: datetime) -> str | None:
    day = now.astimezone(KST_ZONE).date()
    if day.weekday() >= 5:
        return '한국장 주말 휴장'
    # 2026 KRX holiday set needed by current beta/RC operations. Keep this
    # explicit until an official KRX calendar provider is wired into the cache.
    special_2026 = {
        date(2026, 1, 1): '한국장 휴장(신정)',
        date(2026, 2, 16): '한국장 휴장(설 연휴)',
        date(2026, 2, 17): '한국장 휴장(설날)',
        date(2026, 2, 18): '한국장 휴장(설 연휴)',
        date(2026, 3, 2): '한국장 휴장(삼일절 대체공휴일)',
        date(2026, 5, 5): '한국장 휴장(어린이날)',
        date(2026, 5, 25): '한국장 휴장(부처님오신날)',
        date(2026, 8, 17): '한국장 휴장(광복절 대체공휴일)',
        date(2026, 9, 24): '한국장 휴장(추석 연휴)',
        date(2026, 9, 25): '한국장 휴장(추석)',
        date(2026, 9, 28): '한국장 휴장(추석 대체공휴일)',
        date(2026, 10, 5): '한국장 휴장(개천절 대체공휴일)',
        date(2026, 10, 9): '한국장 휴장(한글날)',
        date(2026, 12, 25): '한국장 휴장(성탄절)',
        date(2026, 12, 31): '한국장 휴장(연말 휴장)',
    }
    return special_2026.get(day)


def in_market_window(now: datetime, zone: ZoneInfo, start: time, end: time) -> bool:
    local = now.astimezone(zone)
    current = local.time()
    return start <= current <= end


def is_us_market_active(now: datetime, start: time = time(9, 30), end: time = time(17, 30)) -> bool:
    return us_market_closed_reason(now) is None and in_market_window(now, NY_ZONE, start, end)


def is_kr_market_active(now: datetime, start: time = time(9, 30), end: time = time(16, 40)) -> bool:
    return kr_market_closed_reason(now) is None and in_market_window(now, KST_ZONE, start, end)
