"""One offline CPI-preview expiry policy for RSS selection and Pages validation."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

CPI_PREVIEW_WINDOW = timedelta(days=3)
CPI_PATTERN = re.compile(r'\bcpi\b|소비자\s*물가|inflation\s+(?:data|report|release)', re.I)
PREVIEW_PATTERN = re.compile(
    r'\b(?:brace(?:s|d|ing)?\s+for|ahead\s+of|await(?:s|ed|ing)?|before|preview)\b'
    r'|앞두|대기|예고|발표\s*전', re.I,
)
MONTH_NAMES = (
    'january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december',
)


def parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def cpi_reference_periods(headline: str) -> set[tuple[int | None, int]]:
    periods = {
        (int(year) if year else None, int(month)) for year, month in re.findall(
            r'(?:(\d{4})년\s*)?(?<!\d)(1[0-2]|[1-9])월\s*(?:cpi|소비자\s*물가|물가)', headline, re.I,
        )
    }
    for number, name in enumerate(MONTH_NAMES, 1):
        month = name + '|' + name[:3] + r'\.?'
        for pattern in (
            rf'(?:(\d{{4}})\s+)?\b(?:{month})\s+(?:(\d{{4}})\s+)?(?:cpi|inflation|consumer\s+price)\b',
            rf'\bcpi\s+(?:preview\s+)?for\s+(?:(\d{{4}})\s+)?(?:{month})\b(?:\s+(\d{{4}}))?',
        ):
            for match in re.finditer(pattern, headline, re.I):
                years = [int(year) for year in match.groups() if year]
                if years:
                    periods.update((year, number) for year in years)
                else:
                    periods.add((None, number))
    return periods


def expired_us_cpi_preview(headline: str, published_at: object, now: datetime) -> bool:
    """Never infer a future event from an awaiting-official payload's nextRelease.

    A generic preview is useful only in the existing three-day lead-up window.
    At the scheduled release it expires, even if official results are delayed.
    Re-published previews between releases and explicit old-period previews do
    not become timely merely because their RSS publication timestamp is new.
    """
    text = str(headline or '')
    if not CPI_PATTERN.search(text) or not PREVIEW_PATTERN.search(text):
        return False
    # Do not apply the US release calendar to clearly foreign-only reports.
    foreign = re.search(r'한국|국내|중국|일본|영국|유로존|독일|캐나다|호주|\b(?:korea|china|japan|uk|britain|eurozone|germany|canada|australia)\b', text, re.I)
    us = re.search(r'미국|\bu\.?s\.?\b|\bamerican\b', text, re.I)
    if foreign and not us:
        return False

    # This is the canonical rolling calendar already used for official results.
    # Import lazily so the snapshot builder need not depend on the RSS module.
    from polarmeter_cache_snapshot import SCHEDULED_MACRO_EVENTS

    current = now.astimezone(timezone.utc)
    schedule = sorted(
        (release, label)
        for timestamp, label in SCHEDULED_MACRO_EVENTS['us_cpi']
        if (release := parse_timestamp(timestamp)) is not None
    )
    upcoming = next((item for item in schedule if item[0] > current), None)
    if upcoming is None or upcoming[0] - current > CPI_PREVIEW_WINDOW:
        return True
    published = parse_timestamp(published_at)
    if published is None or published > current:
        return True
    completed = [release for release, _ in schedule if release <= current]
    if completed and published <= completed[-1]:
        return True
    expected_month = int(re.match(r'(\d+)월', upcoming[1]).group(1))
    expected_year = upcoming[0].year - int(expected_month > upcoming[0].month)
    return any(
        month != expected_month or (year is not None and year != expected_year)
        for year, month in cpi_reference_periods(text)
    )
