"""Validity of a retained last close, separate from age/confidence labels."""
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from polarmeter_market_calendar import (
    KST_ZONE, NY_ZONE,
    last_completed_session_date,
    market_session_context,
)

KST = ZoneInfo('Asia/Seoul')

MAX_STALE_SIGNAL_AGE_HOURS = {
    'sp500': 72, 'nasdaq100': 72, 'iwm': 72, 'soxx': 72, 'smh': 72,
    'eem': 72, 'vix': 72, 'usd_krw': 72, 'wti': 72,
    'kospi': 96, 'kosdaq': 96, 'kr_samsung': 96, 'us10y': 96,
    'dxy': 96, 'gold': 96,
}
KR_SESSION_KEYS = frozenset({'kospi', 'kosdaq'})
US_SESSION_KEYS = frozenset({'sp500', 'nasdaq100', 'iwm', 'soxx', 'smh', 'eem', 'vix'})


def last_close_time(signal: dict, now: datetime | None = None) -> datetime | None:
    raw = signal.get('dataAsOf') if 'dataAsOf' in signal else signal.get('asOf')
    current = now or datetime.now(timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    try:
        if re.fullmatch(r'\d{8}|\d{4}-\d{2}-\d{2}', raw):
            observed = datetime.strptime(raw, '%Y%m%d' if len(raw) == 8 else '%Y-%m-%d')
            if observed.date() > current.astimezone(KST).date():
                return None
            return observed.replace(tzinfo=timezone.utc)
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d{1,6})?)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)', raw):
            return None
        observed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if observed.tzinfo is None or observed > current:
            return None
        return observed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def valid_last_close(signal: dict, now: datetime | None = None) -> bool:
    if signal.get('status') not in {'ok', 'stale', 'suspect'} or signal.get('valuePolicy') not in (None, 'show'):
        return False
    value = signal.get('value') if 'value' in signal else signal.get('price')
    pct = signal.get('changePct')
    if isinstance(value, bool) or isinstance(pct, bool) or value in (None, '') or pct in (None, ''):
        return False
    decimal = r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?'
    for field in (value, pct):
        if not isinstance(field, (str, int, float)):
            return False
        if isinstance(field, str) and not re.fullmatch(decimal, field.strip()):
            return False
    try:
        price, change_pct = float(value), float(pct)
    except (TypeError, ValueError, OverflowError):
        return False
    return (math.isfinite(price) and price > 0 and math.isfinite(change_pct)
            and change_pct > -100 and last_close_time(signal, now) is not None)


def session_close_reference(key: str, signal: dict, now: datetime | None = None) -> dict | None:
    """Retain only a valid quote from exactly the last completed session.

    This is not an assertion that an intraday quote is an official settlement.
    Keep its original observation time and disclose a reference, not live data.
    FX, rates and commodities must never inherit an equity holiday exception.
    """
    current = now or datetime.now(timezone.utc)
    if not valid_last_close(signal, current):
        return None
    return session_date_reference(key, signal, current)


def session_date_reference(key: str, signal: dict, now: datetime | None = None) -> dict | None:
    """Calendar/date check only; audits validate numerical fields separately."""
    market = 'KR' if key in KR_SESSION_KEYS else 'US' if key in US_SESSION_KEYS else None
    current = now or datetime.now(timezone.utc)
    if market is None or last_close_time(signal, current) is None:
        return None
    zone = KST_ZONE if market == 'KR' else NY_ZONE
    raw = signal.get('dataAsOf') if 'dataAsOf' in signal else signal.get('asOf')
    raw = str(raw).strip()
    date_only = bool(re.fullmatch(r'\d{8}|\d{4}-\d{2}-\d{2}', raw))
    if date_only:
        # US date-only strings have no unambiguous timezone contract yet.
        # Admit them only when the source explicitly declares an exchange date.
        if market == 'US' and signal.get('observationDateBasis') != 'exchange_date':
            return None
        observed_date = datetime.strptime(raw, '%Y%m%d' if len(raw) == 8 else '%Y-%m-%d').date()
    else:
        observed_date = last_close_time(signal, current).astimezone(zone).date()
    completed = last_completed_session_date(current, market)
    if completed is None or observed_date != completed:
        return None
    phase = market_session_context(current, market)['phase']
    labels = {
        'closed': '휴장 중 마지막 거래일 기준값 유지',
        'pre_open': '개장 전 마지막 거래일 기준값 유지',
        'open': '장중 갱신 지연 · 마지막 거래일 기준값 유지',
        'after_close': '마감 자료 확인 중 · 마지막 거래일 기준값 유지',
    }
    return {
        'version': 'last-completed-session-v1', 'key': key, 'market': market,
        'observationDate': observed_date.isoformat(), 'completedSessionDate': completed.isoformat(),
        'referenceAsOf': current.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'dateBasis': 'exchange_date' if date_only else 'timestamp',
        'phase': phase, 'label': labels[phase],
    }
