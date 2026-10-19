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

# Official NYSE cash-equity calendar, checked 2026-10-19.
# https://www.nyse.com/trade/hours-calendars
# Published 2026-2028 dates only; do not infer July/Christmas half-days
# from a generic weekday rule (some are full holidays instead).
US_EQUITY_EARLY_CLOSE_DATES = frozenset({
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
    date(2028, 7, 3), date(2028, 11, 24),
})


def us_equity_close_time(now: datetime) -> time:
    """Core cash-equity session close, not options or extended trading."""
    return time(13) if now.astimezone(NY_ZONE).date() in US_EQUITY_EARLY_CLOSE_DATES else time(16)


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
        date(2026, 5, 1): '한국장 휴장(근로자의날)',
        date(2026, 5, 5): '한국장 휴장(어린이날)',
        date(2026, 5, 25): '한국장 휴장(부처님오신날)',
        date(2026, 6, 3): '한국장 휴장(전국동시지방선거)',
        date(2026, 8, 17): '한국장 휴장(광복절 대체공휴일)',
        date(2026, 9, 24): '한국장 휴장(추석 연휴)',
        date(2026, 9, 25): '한국장 휴장(추석)',
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


def last_completed_session_date(now: datetime, market: str) -> date | None:
    """Shared delayed-feed baseline; never guess an unsupported KR calendar."""
    if market not in {'KR', 'US'}:
        return None
    zone = KST_ZONE if market == 'KR' else NY_ZONE
    closed = kr_market_closed_reason if market == 'KR' else us_market_closed_reason
    local = now.astimezone(zone)
    if market == 'KR' and local.year != 2026:
        return None
    candidate = local.date()
    # Preserve the existing KR delayed-feed boundary. US feeds retain their
    # 45-minute arrival grace, measured from the actual session close.
    cutoff = time(16, 45) if market == 'KR' else (
        datetime.combine(candidate, us_equity_close_time(local), tzinfo=zone) + timedelta(minutes=45)
    ).time()
    if closed(local) is None and local.time() >= cutoff:
        return candidate
    for _ in range(32):
        candidate -= timedelta(days=1)
        if market == 'KR' and candidate.year != 2026:
            return None
        if closed(datetime.combine(candidate, time(12), tzinfo=zone)) is None:
            return candidate
    return None


def market_session_context(now: datetime, market: str) -> dict | None:
    """Calendar context independent of whether any received quote is usable."""
    if market not in {'KR', 'US'}:
        return None
    zone = KST_ZONE if market == 'KR' else NY_ZONE
    local = now.astimezone(zone)
    if market == 'KR' and local.year != 2026:
        return None
    reason = (kr_market_closed_reason if market == 'KR' else us_market_closed_reason)(now)
    minutes = local.hour * 60 + local.minute
    close = time(15, 30) if market == 'KR' else us_equity_close_time(local)
    opening, closing = (540 if market == 'KR' else 570), close.hour * 60 + close.minute
    phase = ('closed' if reason else 'pre_open' if minutes < opening
             else 'open' if minutes < closing else 'after_close')
    completed = last_completed_session_date(now, market)
    return {'version': 'market-session-v1', 'market': market,
            'referenceAsOf': now.astimezone(ZoneInfo('UTC')).isoformat().replace('+00:00', 'Z'),
            'localDate': local.date().isoformat(), 'phase': phase,
            'closedKind': ('weekend' if local.weekday() >= 5 else 'holiday') if reason else None,
            'completedSessionDate': completed.isoformat() if completed else None}


def is_us_market_active(now: datetime, start: time = time(9, 30), end: time = time(17, 30)) -> bool:
    """Freshness-watch window, including the caller's post-close grace.

    The default 17:30 check is 90 minutes after a normal 16:00 close; on
    a 13:00 early close the same check ends at 14:30. This is deliberately
    separate from the public regular-session phase, which ends at 13:00.
    """
    if us_market_closed_reason(now) is not None:
        return False
    close = us_equity_close_time(now)
    if close < time(16):
        end_minutes = end.hour * 60 + end.minute
        grace_minutes = max(0, end_minutes - 16 * 60)
        shifted_end = min(end_minutes, close.hour * 60 + close.minute + grace_minutes)
        end = time(shifted_end // 60, shifted_end % 60, end.second, end.microsecond)
    return in_market_window(now, NY_ZONE, start, end)


def is_kr_market_active(now: datetime, start: time = time(9, 30), end: time = time(16, 40)) -> bool:
    return kr_market_closed_reason(now) is None and in_market_window(now, KST_ZONE, start, end)
