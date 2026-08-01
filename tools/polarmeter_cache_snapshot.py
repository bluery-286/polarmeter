#!/usr/bin/env python3
"""Build a provider-neutral free-cache snapshot for PolarMeter.

The snapshot is the cache-server contract. It can be built from a provider probe
report or, while keys are unavailable, as a safe placeholder that makes missing
coverage explicit without calling paid providers.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import tempfile
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from polarmeter_market_calendar import is_kr_market_active, is_us_market_active

WORKSPACE = Path(__file__).resolve().parents[1]
PROJECT = WORKSPACE
DEFAULT_PROBE = PROJECT / 'testflight/free-provider-probe-report-latest.json'
DEFAULT_OUTPUT = PROJECT / 'testflight/free-cache-snapshot-latest.json'
DEFAULT_NEWS_PROBE = PROJECT / 'testflight/news-rss-probe-latest.json'
DEFAULT_LAST_KNOWN_GOOD = PROJECT / 'testflight/last-known-good-snapshot.json'
NEWS_RECOMMENDED_SCHEDULE = '30min_weekdays_60min_weekends_public_headline_cache'
NEWS_SNAPSHOT_MAX_ITEMS = 30
MAX_STALE_SIGNAL_AGE_HOURS = {
    # Display-facing market values may be delayed, but multi-day-old numbers must
    # not be recycled as current.
    'sp500': 72,
    'nasdaq100': 72,
    'iwm': 72,
    'soxx': 72,
    'smh': 72,
    'eem': 72,
    'vix': 72,
    'usd_krw': 72,
    'wti': 72,
    'kospi': 96,
    'kosdaq': 96,
    'us10y': 96,
    'dxy': 96,
    'gold': 96,
}
KST = timezone(timedelta(hours=9))
KR_INTRADAY_STALE_KEYS = {'kospi', 'kosdaq', 'kr_samsung'}
ACTIVE_MARKET_MAX_AGE_HOURS = 3.0
KR_ACTIVE_MARKET_STALE_KEYS = {'kospi', 'kosdaq', 'usd_krw'}
US_ACTIVE_MARKET_STALE_KEYS = {
    'sp500', 'nasdaq100', 'iwm', 'soxx', 'smh', 'eem', 'vix',
    'us10y', 'dxy', 'wti', 'gold',
}
MACRO_RELEASE_GRACE = timedelta(hours=2)
MACRO_OFFICIAL_FETCH_DELAY = timedelta(minutes=5)
FEDERAL_RESERVE_HOST = 'https://www.federalreserve.gov'
BLS_API_URL = 'https://api.bls.gov/publicAPI/v2/timeseries/data/'
BLS_RELEASE_URLS = {
    'us_cpi': 'https://www.bls.gov/news.release/cpi.nr0.htm',
    'us_nonfarm_payrolls': 'https://www.bls.gov/news.release/empsit.nr0.htm',
}
BLS_SERIES = {
    'cpi_sa': 'CUSR0000SA0',
    'cpi_nsa': 'CUUR0000SA0',
    'nonfarm_payrolls': 'CES0000000001',
    'unemployment_rate': 'LNS14000000',
}
SCHEDULED_MACRO_EVENTS = {
    'us_nonfarm_payrolls': [
        ('2026-07-02T12:30:00Z', '6월 고용'),
        ('2026-08-07T12:30:00Z', '7월 고용'),
        ('2026-09-04T12:30:00Z', '8월 고용'),
        ('2026-10-02T12:30:00Z', '9월 고용'),
        ('2026-11-06T13:30:00Z', '10월 고용'),
        ('2026-12-04T13:30:00Z', '11월 고용'),
    ],
    'us_cpi': [
        ('2026-07-14T12:30:00Z', '6월 CPI'),
        ('2026-08-12T12:30:00Z', '7월 CPI'),
        ('2026-09-11T12:30:00Z', '8월 CPI'),
        ('2026-10-14T12:30:00Z', '9월 CPI'),
        ('2026-11-10T13:30:00Z', '10월 CPI'),
        ('2026-12-10T13:30:00Z', '11월 CPI'),
    ],
    'fomc_rate': [
        ('2026-06-17T18:00:00Z', '6월 FOMC'),
        ('2026-07-29T18:00:00Z', '7월 FOMC'),
        ('2026-09-16T18:00:00Z', '9월 FOMC'),
        ('2026-10-28T18:00:00Z', '10월 FOMC'),
        ('2026-12-09T19:00:00Z', '12월 FOMC'),
        ('2027-01-27T19:00:00Z', '1월 FOMC'),
        ('2027-03-17T18:00:00Z', '3월 FOMC'),
        ('2027-04-28T18:00:00Z', '4월 FOMC'),
        ('2027-06-09T18:00:00Z', '6월 FOMC'),
        ('2027-07-28T18:00:00Z', '7월 FOMC'),
        ('2027-09-15T18:00:00Z', '9월 FOMC'),
        ('2027-10-27T18:00:00Z', '10월 FOMC'),
        ('2027-12-08T19:00:00Z', '12월 FOMC'),
    ],
}
LAST_MACRO_RELEASES = {
    'us_nonfarm_payrolls': {
        'label': '6월 고용',
        'releasedAt': '2026-07-02T12:30:00Z',
        'resultLabel': '일자리 +5.7만명 · 실업률 4.2%',
        'detail': '지난 발표에서는 일자리 증가가 둔화됐고 실업률은 4.2%였습니다. 고용 둔화가 금리 기대를 낮추는지, 경기 걱정을 키우는지 함께 봅니다.',
        'sourceLabel': 'BLS Employment Situation · 2026-07-02',
        'sourceUrl': 'https://www.bls.gov/news.release/empsit.nr0.htm',
    },
    'us_cpi': {
        'label': '6월 CPI',
        'releasedAt': '2026-07-14T12:30:00Z',
        'resultLabel': '물가 전월 대비 -0.4% · 전년 대비 +3.5%',
        'detail': '6월 물가는 에너지 가격 하락 영향으로 전월 대비 0.4% 내렸습니다. 물가 부담은 완화됐지만 전년 대비 3.5%라 금리 부담이 더 낮아지는지 이어서 봅니다.',
        'sourceLabel': 'BLS CPI · 2026-07-14',
        'sourceUrl': 'https://www.bls.gov/news.release/cpi.nr0.htm',
        'burdenScore': 46,
    },
    'fomc_rate': {
        'label': '7월 FOMC',
        'releasedAt': '2026-07-29T18:00:00Z',
        'resultLabel': '기준금리 3.50~3.75% 동결',
        'detail': '미국 중앙은행은 금리를 유지했습니다. 표결은 9대3이었고, 반대 3명은 0.25%포인트 인상을 원해 긴축 압력이 남았습니다.',
        'sourceLabel': 'Federal Reserve FOMC · 2026-07-29',
        'sourceUrl': 'https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm',
    },
}

CORE_SIGNALS = {'kospi', 'usd_krw', 'sp500', 'vix', 'wti'}
CORE_GROUPS = {
    'kr_index': ['kospi', 'kosdaq'],
    'fx': ['usd_krw'],
    'us_index': ['sp500', 'nasdaq100'],
    'volatility': ['vix'],
    'commodity': ['wti', 'gold'],
    'rate': ['us10y'],
    'dollar': ['dxy'],
}

SANITY_RANGES = {
    # 2026 POC cross-check: KOSPI genuinely trades in the 7,000~8,000 range.
    # Displayed index values remain temperature inputs even when low-confidence:
    # range/change violations publish as suspect/show, not invalid/hide.
    # B2.67: a 6% KOSPI move is already an exceptional intraday condition.
    # Keep the value in temperature, but disclose low confidence until corroborated.
    'kospi': {'minPrice': 1000, 'maxPrice': 12000, 'priceOutOfRangeStatus': 'suspect', 'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 18.0, 'rejectAbsChangeStatus': 'suspect', 'requiresChangePct': True},
    'kosdaq': {'minPrice': 400, 'maxPrice': 1500, 'priceOutOfRangeStatus': 'suspect', 'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 16.0, 'rejectAbsChangeStatus': 'suspect', 'requiresChangePct': True},
    'sp500': {'suspectAbsChangePct': 5.0, 'rejectAbsChangePct': 7.0, 'requiresChangePct': True},
    'nasdaq100': {'suspectAbsChangePct': 5.0, 'rejectAbsChangePct': 7.0, 'requiresChangePct': True},
    'usd_krw': {'suspectAbsChangePct': 2.0, 'rejectAbsChangePct': 3.0, 'requiresChangePct': False},
    'us10y': {'minPrice': 1.0, 'maxPrice': 8.0, 'suspectAbsChangePct': 8.0, 'rejectAbsChangePct': 15.0, 'requiresChangePct': False},
    'dxy': {'minPrice': 70.0, 'maxPrice': 140.0, 'suspectAbsChangePct': 3.0, 'rejectAbsChangePct': 5.0, 'requiresChangePct': False},
    'vix': {'suspectAbsChangePct': 12.0, 'rejectAbsChangePct': 20.0, 'requiresChangePct': False},
    'wti': {'suspectAbsChangePct': 8.0, 'rejectAbsChangePct': 12.0, 'requiresChangePct': False},
    'gold': {'suspectAbsChangePct': 3.0, 'rejectAbsChangePct': 5.0, 'requiresChangePct': False},
    'soxx': {'minPrice': 100, 'maxPrice': 1200, 'priceOutOfRangeStatus': 'suspect', 'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 16.0, 'rejectAbsChangeStatus': 'suspect', 'requiresChangePct': False},
    'smh': {'minPrice': 50, 'maxPrice': 1000, 'priceOutOfRangeStatus': 'suspect', 'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 16.0, 'rejectAbsChangeStatus': 'suspect', 'requiresChangePct': False},
    'iwm': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 9.0, 'rejectAbsChangeStatus': 'suspect', 'requiresChangePct': False},
    'eem': {'suspectAbsChangePct': 6.0, 'rejectAbsChangePct': 9.0, 'rejectAbsChangeStatus': 'suspect', 'requiresChangePct': False},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def normalized_as_of(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
    if value:
        return str(value)
    return utc_now()


def parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, str) and len(value.strip()) == 8 and value.strip().isdigit():
            parsed = datetime.strptime(value.strip(), '%Y%m%d')
            return parsed.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _rate_token_to_float(value: str) -> float:
    token = str(value or '').strip().replace('‑', '-').replace('–', '-').replace('—', '-')
    mixed = re.fullmatch(r'(\d+)-(\d+)/(\d+)', token)
    if mixed:
        whole, numerator, denominator = (int(part) for part in mixed.groups())
        return whole + numerator / denominator
    fraction = re.fullmatch(r'(\d+)/(\d+)', token)
    if fraction:
        numerator, denominator = (int(part) for part in fraction.groups())
        return numerator / denominator
    return float(token)


def _clean_official_html(value: str) -> str:
    text = re.sub(r'<script\b[^>]*>.*?</script>', ' ', value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<style\b[^>]*>.*?</style>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', html.unescape(text)).strip()


def parse_fomc_statement(
    statement_html: str,
    *,
    label: str,
    released_at: datetime,
    source_url: str,
) -> dict[str, Any]:
    text = _clean_official_html(statement_html).replace('‑', '-').replace('–', '-').replace('—', '-')
    decision_match = re.search(
        r'decided to\s+(maintain|raise|lower)\s+the target range for the federal funds rate\s+at\s+'
        r'(\d+(?:-\d+/\d+|\.\d+)?|\d+/\d+)\s+to\s+'
        r'(\d+(?:-\d+/\d+|\.\d+)?|\d+/\d+)\s+percent',
        text,
        flags=re.IGNORECASE,
    )
    if not decision_match:
        raise ValueError('official FOMC statement target range was not found')
    action, low_raw, high_raw = decision_match.groups()
    low = _rate_token_to_float(low_raw)
    high = _rate_token_to_float(high_raw)
    action_ko = {'maintain': '동결', 'raise': '인상', 'lower': '인하'}[action.lower()]
    result_label = f'기준금리 {low:.2f}~{high:.2f}% {action_ko}'

    detail = f'미국 중앙은행은 기준금리를 {low:.2f}~{high:.2f}%로 {action_ko}했습니다.'
    vote_match = re.search(r'approved .*? by a\s+(\d+)\s*-\s*(\d+)\s+vote', text, flags=re.IGNORECASE)
    dissent_match = re.search(
        r'who preferred to\s+(raise|lower|maintain)\s+the target range.*?by\s+'
        r'(\d+(?:-\d+/\d+|\.\d+)?|\d+/\d+)\s+percentage point',
        text,
        flags=re.IGNORECASE,
    )
    if vote_match:
        detail += f' 표결은 {vote_match.group(1)}대{vote_match.group(2)}였습니다.'
    if dissent_match:
        dissent_action, move_raw = dissent_match.groups()
        dissent_ko = {'raise': '인상', 'lower': '인하', 'maintain': '동결'}[dissent_action.lower()]
        move = _rate_token_to_float(move_raw)
        detail += f' 반대 의견은 {move:.2f}%포인트 {dissent_ko}을 원했습니다.'

    return {
        'label': label,
        'releasedAt': released_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'resultLabel': result_label,
        'detail': detail,
        'sourceLabel': f'Federal Reserve FOMC · {released_at.date().isoformat()}',
        'sourceUrl': source_url,
    }


def fetch_official_fomc_release(
    released_at: datetime,
    label: str,
    *,
    fetch_text: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    release_date = released_at.astimezone(timezone.utc).date()
    source_url = (
        f'{FEDERAL_RESERVE_HOST}/newsevents/pressreleases/'
        f'monetary{release_date.strftime("%Y%m%d")}a.htm'
    )
    if fetch_text is None:
        def fetch_text(url: str) -> str:
            request = urllib.request.Request(
                url,
                headers={
                    'User-Agent': 'PolarMeter official macro release checker',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read().decode('utf-8', errors='replace')
    return parse_fomc_statement(
        fetch_text(source_url),
        label=label,
        released_at=released_at,
        source_url=source_url,
    )


def _reference_period(label: str, released_at: datetime) -> tuple[int, int]:
    month_match = re.search(r'(\d{1,2})월', str(label or ''))
    if not month_match:
        raise ValueError(f'macro release label has no reference month: {label}')
    month = int(month_match.group(1))
    if not 1 <= month <= 12:
        raise ValueError(f'macro release label has invalid reference month: {label}')
    released = released_at.astimezone(timezone.utc)
    year = released.year if month <= released.month else released.year - 1
    return year, month


def _previous_period(year: int, month: int, months: int = 1) -> tuple[int, int]:
    absolute = year * 12 + month - 1 - months
    return absolute // 12, absolute % 12 + 1


def fetch_official_bls_series(
    series_ids: list[str],
    start_year: int,
    end_year: int,
) -> dict[str, Any]:
    request_body = json.dumps({
        'seriesid': series_ids,
        'startyear': str(start_year),
        'endyear': str(end_year),
    }).encode('utf-8')
    request = urllib.request.Request(
        BLS_API_URL,
        data=request_body,
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'PolarMeter official macro release checker',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode('utf-8'))


def _bls_series_points(payload: dict[str, Any]) -> dict[str, dict[tuple[int, int], float]]:
    if payload.get('status') != 'REQUEST_SUCCEEDED':
        raise ValueError(f'BLS API request failed: {payload.get("message")}')
    output: dict[str, dict[tuple[int, int], float]] = {}
    for series in (payload.get('Results') or {}).get('series') or []:
        series_id = str(series.get('seriesID') or '')
        points: dict[tuple[int, int], float] = {}
        for item in series.get('data') or []:
            period = str(item.get('period') or '')
            if not re.fullmatch(r'M(0[1-9]|1[0-2])', period):
                continue
            try:
                point = (int(item.get('year')), int(period[1:]))
                points[point] = float(item.get('value'))
            except (TypeError, ValueError):
                continue
        if series_id:
            output[series_id] = points
    return output


def _required_bls_value(
    points: dict[str, dict[tuple[int, int], float]],
    series_id: str,
    period: tuple[int, int],
) -> float:
    value = (points.get(series_id) or {}).get(period)
    if value is None:
        raise ValueError(f'BLS series {series_id} is missing period {period[0]}-{period[1]:02d}')
    return value


def _signed_percent(value: float) -> str:
    rounded = round(value, 1)
    if rounded == 0:
        rounded = 0.0
    return f'{rounded:+.1f}%'


def _signed_job_change(delta_thousands: float) -> str:
    ten_thousands = round(delta_thousands / 10.0, 1)
    if ten_thousands == 0:
        ten_thousands = 0.0
    return f'{ten_thousands:+.1f}만명'


def parse_bls_cpi_release(
    payload: dict[str, Any],
    *,
    label: str,
    released_at: datetime,
    source_url: str,
) -> dict[str, Any]:
    year, month = _reference_period(label, released_at)
    current = (year, month)
    previous = _previous_period(year, month)
    year_ago = (year - 1, month)
    points = _bls_series_points(payload)
    current_sa = _required_bls_value(points, BLS_SERIES['cpi_sa'], current)
    previous_sa = _required_bls_value(points, BLS_SERIES['cpi_sa'], previous)
    current_nsa = _required_bls_value(points, BLS_SERIES['cpi_nsa'], current)
    year_ago_nsa = _required_bls_value(points, BLS_SERIES['cpi_nsa'], year_ago)
    month_change = (current_sa / previous_sa - 1.0) * 100.0
    year_change = (current_nsa / year_ago_nsa - 1.0) * 100.0
    burden_score = int(max(0, min(100, round(50 + month_change * 20 + (year_change - 2.0) * 3))))
    return {
        'label': label,
        'releasedAt': released_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'resultLabel': f'물가 전월 대비 {_signed_percent(month_change)} · 전년 대비 {_signed_percent(year_change)}',
        'detail': (
            f'{label} 공식 지수는 전월 대비 {_signed_percent(month_change)}, '
            f'전년 대비 {_signed_percent(year_change)}였습니다. '
            '금리 부담이 낮아지는지와 달러·환율 반응을 함께 봅니다.'
        ),
        'sourceLabel': f'BLS CPI · {released_at.date().isoformat()}',
        'sourceUrl': source_url,
        'burdenScore': burden_score,
    }


def parse_bls_employment_release(
    payload: dict[str, Any],
    *,
    label: str,
    released_at: datetime,
    source_url: str,
) -> dict[str, Any]:
    year, month = _reference_period(label, released_at)
    current = (year, month)
    previous = _previous_period(year, month)
    points = _bls_series_points(payload)
    current_jobs = _required_bls_value(points, BLS_SERIES['nonfarm_payrolls'], current)
    previous_jobs = _required_bls_value(points, BLS_SERIES['nonfarm_payrolls'], previous)
    unemployment = _required_bls_value(points, BLS_SERIES['unemployment_rate'], current)
    change_thousands = current_jobs - previous_jobs
    return {
        'label': label,
        'releasedAt': released_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'resultLabel': f'일자리 {_signed_job_change(change_thousands)} · 실업률 {unemployment:.1f}%',
        'detail': (
            f'{label} 비농업 일자리는 전월보다 {_signed_job_change(change_thousands)} 변했고, '
            f'실업률은 {unemployment:.1f}%였습니다. '
            '고용 방향이 금리 기대와 경기 부담 중 어느 쪽을 키우는지 함께 봅니다.'
        ),
        'sourceLabel': f'BLS Employment Situation · {released_at.date().isoformat()}',
        'sourceUrl': source_url,
    }


def fetch_official_bls_release(
    key: str,
    released_at: datetime,
    label: str,
    *,
    fetch_series: Callable[[list[str], int, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    year, _month = _reference_period(label, released_at)
    fetcher = fetch_series or fetch_official_bls_series
    if key == 'us_cpi':
        series_ids = [BLS_SERIES['cpi_sa'], BLS_SERIES['cpi_nsa']]
        payload = fetcher(series_ids, year - 1, year)
        return parse_bls_cpi_release(
            payload,
            label=label,
            released_at=released_at,
            source_url=BLS_RELEASE_URLS[key],
        )
    if key == 'us_nonfarm_payrolls':
        series_ids = [BLS_SERIES['nonfarm_payrolls'], BLS_SERIES['unemployment_rate']]
        payload = fetcher(series_ids, year - 1, year)
        return parse_bls_employment_release(
            payload,
            label=label,
            released_at=released_at,
            source_url=BLS_RELEASE_URLS[key],
        )
    raise ValueError(f'unsupported BLS macro release key: {key}')


def build_macro_events(
    now: datetime | None = None,
    *,
    fomc_fetcher: Callable[[str], str] | None = None,
    bls_fetcher: Callable[[list[str], int, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    events: dict[str, Any] = {}
    for key, schedule in SCHEDULED_MACRO_EVENTS.items():
        parsed_schedule = [
            (parsed, label)
            for iso_value, label in schedule
            if (parsed := parse_utc_datetime(iso_value)) is not None
        ]
        last_release = dict(LAST_MACRO_RELEASES.get(key) or {})
        released_at = parse_utc_datetime(last_release.get('releasedAt'))
        fetch_due = [item for item in parsed_schedule if item[0] <= current - MACRO_OFFICIAL_FETCH_DELAY]
        unresolved: tuple[datetime, str] | None = None
        if fetch_due and (released_at is None or released_at < fetch_due[-1][0]):
            unresolved = fetch_due[-1]
            if key == 'fomc_rate':
                try:
                    last_release = fetch_official_fomc_release(
                        unresolved[0],
                        unresolved[1],
                        fetch_text=fomc_fetcher,
                    )
                    released_at = parse_utc_datetime(last_release.get('releasedAt'))
                    unresolved = None
                except Exception:
                    if unresolved[0] <= current - MACRO_RELEASE_GRACE:
                        raise
            elif key in BLS_RELEASE_URLS:
                try:
                    last_release = fetch_official_bls_release(
                        key,
                        unresolved[0],
                        unresolved[1],
                        fetch_series=bls_fetcher,
                    )
                    released_at = parse_utc_datetime(last_release.get('releasedAt'))
                    unresolved = None
                except Exception:
                    if unresolved[0] <= current - MACRO_RELEASE_GRACE:
                        raise
            elif unresolved[0] <= current - MACRO_RELEASE_GRACE:
                raise AssertionError(
                    f'macro release stale after official event: '
                    f'{key} due={unresolved[0].isoformat()} releasedAt={last_release.get("releasedAt")}'
                )
        upcoming = next((item for item in parsed_schedule if item[0] > current), None)
        if unresolved is not None:
            upcoming = unresolved
        events[key] = {
            'status': 'awaiting_official' if unresolved is not None else ('ok' if last_release else 'unavailable'),
            'sourcePolicy': 'official_release_registry_with_expiry_gate',
            'lastRelease': last_release or None,
            'nextRelease': {
                'scheduledAt': upcoming[0].isoformat().replace('+00:00', 'Z'),
                'label': upcoming[1],
            } if upcoming else None,
        }
    return events


def candidate_data_date_kst(value: Any) -> Any:
    raw = str(value or '').strip()
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, '%Y%m%d').date()
        except ValueError:
            return None
    parsed = parse_utc_datetime(value)
    if not parsed:
        return None
    return parsed.astimezone(KST).date()


def is_kr_intraday_guard_active(now: datetime | None = None) -> bool:
    current = (now or datetime.now(timezone.utc)).astimezone(KST)
    if current.weekday() >= 5:
        return False
    return time(9, 30) <= current.time() <= time(16, 40)


def kr_intraday_stale_reason(key: str, provider: str, item: dict[str, Any]) -> str | None:
    if key not in KR_INTRADAY_STALE_KEYS or not is_kr_intraday_guard_active():
        return None
    data_date = candidate_data_date_kst(item.get('asOf'))
    current_date = datetime.now(timezone.utc).astimezone(KST).date()
    if data_date is not None and data_date < current_date:
        return f'kr_intraday_prior_day_data provider={provider} dataAsOf={item.get("asOf")}'
    return None


def active_market_stale_reason(key: str, provider: str, item: dict[str, Any]) -> str | None:
    now = datetime.now(timezone.utc)
    if key in KR_ACTIVE_MARKET_STALE_KEYS and not is_kr_market_active(now, time(9, 30), time(16, 40)):
        return None
    if key in US_ACTIVE_MARKET_STALE_KEYS and not is_us_market_active(now, time(9, 30), time(17, 30)):
        return None
    if key not in KR_ACTIVE_MARKET_STALE_KEYS and key not in US_ACTIVE_MARKET_STALE_KEYS:
        return None
    age_hours = candidate_age_hours(item)
    if age_hours is None or age_hours > ACTIVE_MARKET_MAX_AGE_HOURS:
        return f'active_market_stale provider={provider} dataAsOf={item.get("asOf")} ageHours={age_hours}'
    return None


def hard_stale_reason(key: str, provider: str, item: dict[str, Any]) -> str | None:
    max_age = MAX_STALE_SIGNAL_AGE_HOURS.get(key)
    if max_age is None:
        return None
    age_hours = candidate_age_hours(item)
    if age_hours is None or age_hours > max_age:
        return f'hard_stale provider={provider} dataAsOf={item.get("asOf")} ageHours={age_hours} maxAgeHours={max_age}'
    return None


def stale_signal_age_hours(signal: dict[str, Any]) -> float | None:
    data_as_of = parse_utc_datetime(signal.get('dataAsOf') or signal.get('lastSuccessfulAt') or signal.get('fetchedAt'))
    if not data_as_of:
        return None
    return max(0.0, (datetime.now(timezone.utc) - data_as_of).total_seconds() / 3600)


def candidate_age_hours(item: dict[str, Any]) -> float | None:
    data_as_of = parse_utc_datetime(normalized_as_of(item.get('asOf')))
    if not data_as_of:
        return None
    return max(0.0, (datetime.now(timezone.utc) - data_as_of).total_seconds() / 3600)


def candidate_freshness_rank(item: dict[str, Any]) -> int:
    age_hours = candidate_age_hours(item)
    if age_hours is None:
        return 0
    if age_hours <= 18:
        return 3
    if age_hours <= 84:
        return 2
    if age_hours <= 168:
        return 1
    return 0


def stale_signal_is_too_old(key: str, signal: dict[str, Any]) -> bool:
    max_hours = MAX_STALE_SIGNAL_AGE_HOURS.get(key)
    if max_hours is None:
        return False
    age_hours = stale_signal_age_hours(signal)
    return age_hours is None or age_hours > max_hours


def kr_index_display_badge(value: Any) -> str:
    """Return a user-facing Korean index timing badge from a data timestamp.

    KRX regular trading ends at 15:30 KST. A delayed public-chart timestamp after
    the close should not be labelled as intraday; that creates a trust mismatch
    when the exact data timestamp is shown next to the badge.
    """
    raw = str(value or '').strip()
    if len(raw) == 8 and raw.isdigit():
        return '종가 기준'
    dt = None
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00')) if raw else None
    except ValueError:
        dt = None
    if not dt:
        return '지연 시세'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    kst = dt.astimezone(timezone.utc).timestamp() + 9 * 3600
    local = datetime.fromtimestamp(kst, timezone.utc)
    minutes = local.hour * 60 + local.minute
    if minutes < 9 * 60:
        return '전일 종가'
    if minutes < 15 * 60 + 30:
        return '장중 기준'
    return '종가 기준'


def load_probe(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def load_news_probe(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def load_last_known_good(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return payload.get('signals') if isinstance(payload.get('signals'), dict) else {}
    except Exception:
        return {}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write('\n')
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def provider_items(probe: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {provider.get('provider', 'unknown'): provider.get('items', []) for provider in probe.get('providers', [])}


def provider_metrics(probe: dict[str, Any]) -> dict[str, Any]:
    providers = probe.get('providers') if isinstance(probe.get('providers'), list) else []
    status_by_name: dict[str, str] = {}
    call_count = 0
    item_failure_count = 0

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get('provider') or 'unknown')
        status = str(provider.get('status') or 'unknown')
        status_by_name[name] = status
        items = provider.get('items') if isinstance(provider.get('items'), list) else []
        call_count += len(items)
        item_failure_count += sum(1 for item in items if isinstance(item, dict) and item.get('status') != 'ok')

    provider_failure_count = sum(1 for status in status_by_name.values() if status not in {'ok', 'partial'})
    return {
        'providerCallCount': call_count,
        'providerFailureCount': provider_failure_count,
        'providerItemFailureCount': item_failure_count,
        'providerStatusByName': status_by_name,
    }


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_candidate(key: str, item: dict[str, Any]) -> tuple[str, str | None]:
    if item.get('status') != 'ok' or item.get('price') in (None, ''):
        return 'unavailable', item.get('reason') or 'no_price'
    rule = SANITY_RANGES.get(key, {})
    price = as_float(item.get('price'))
    if price is not None:
        min_price = rule.get('minPrice')
        max_price = rule.get('maxPrice')
        price_out_of_range_status = str(rule.get('priceOutOfRangeStatus') or 'invalid')
        if min_price is not None and price < float(min_price):
            return price_out_of_range_status, f'price_below_range<{min_price}'
        if max_price is not None and price > float(max_price):
            return price_out_of_range_status, f'price_above_range>{max_price}'
    change_pct = as_float(item.get('changePct'))
    if rule.get('requiresChangePct') and change_pct is None:
        return 'partial', 'missing_changePct'
    if change_pct is not None:
        abs_change = abs(change_pct)
        if abs_change > float(rule.get('rejectAbsChangePct', 999)):
            return str(rule.get('rejectAbsChangeStatus') or 'invalid'), f'changePct_out_of_range>{rule.get("rejectAbsChangePct")}'
        if abs_change > float(rule.get('suspectAbsChangePct', 999)):
            return 'suspect', f'changePct_suspect>{rule.get("suspectAbsChangePct")}'
    return 'ok', None


def signal_reliability(key: str, provider: str, status: str, reason: str | None = None, data_as_of: Any = None) -> dict[str, Any]:
    if status == 'stale':
        return {
            'sourceClass': 'stale_last_known_good',
            'displayBadge': '지연된 값 · 참고만',
            'confidencePolicy': 'low_stale_last_known_good',
        }
    if key == 'usd_krw':
        if status == 'suspect':
            return {
                'sourceClass': 'market_rate_large_move',
                'displayBadge': '변동 큼 · 참고용 환율',
                'confidencePolicy': 'low_large_move_until_official_fx',
            }
        official = provider == 'bok-ecos-free'
        return {
            'sourceClass': 'official_reference' if official else 'market_rate',
            'displayBadge': '한국은행 기준환율' if official else '참고용 환율',
            'confidencePolicy': 'normal' if official else 'low_until_official_fx',
        }
    if key == 'vix':
        if status == 'stale' or reason and '429' in reason:
            return {
                'sourceClass': 'stale_last_known_good',
                'displayBadge': '지연된 값 · 참고만',
                'confidencePolicy': 'low_on_provider_rate_limit',
            }
        if status == 'suspect':
            return {
                'sourceClass': 'volatility_index_large_move',
                'displayBadge': '변동 큼 · 확인 전',
                'confidencePolicy': 'low_large_move_until_corroborated',
            }
        return {'sourceClass': 'volatility_index', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}
    if key == 'gold' and status == 'suspect':
        return {
            'sourceClass': 'supplementary_safe_haven',
            'displayBadge': '이상 변동 주의',
            'confidencePolicy': 'supplementary_only',
        }
    if key == 'us10y':
        return {'sourceClass': 'public_rate_index', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}
    if key == 'dxy':
        return {'sourceClass': 'public_dollar_index', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}
    if key in {'iwm', 'eem'}:
        return {'sourceClass': 'diversification_index_etf', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}
    if status == 'suspect' and key in {'kospi', 'kosdaq'} and provider == 'public-chart-delayed':
        return {
            'sourceClass': 'worker_side_public_chart_large_move',
            'displayBadge': '변동 큼 · 공공차트 확인',
            'confidencePolicy': 'low_until_corroborated_public_chart',
        }
    if status == 'suspect':
        return {
            'sourceClass': 'delayed_market_data_large_move',
            'displayBadge': '변동 큼 · 보조 확인',
            'confidencePolicy': 'low_until_corroborated_large_move',
        }
    if key in {'kospi', 'kosdaq'} and provider == 'public-chart-delayed':
        return {'sourceClass': 'worker_side_public_chart_intraday', 'displayBadge': kr_index_display_badge(data_as_of), 'confidencePolicy': 'normal_with_fallback_to_public_close'}
    if key in {'kospi', 'kosdaq'}:
        return {'sourceClass': 'delayed_market_data', 'displayBadge': '공공 지연', 'confidencePolicy': 'normal'}
    return {'sourceClass': 'delayed_market_data', 'displayBadge': '지연 시세', 'confidencePolicy': 'normal'}


def _parse_history_timestamp_ms(value: Any) -> int | None:
    """Require a real UTC-parseable timestamp (not merely non-empty)."""
    if value in (None, ''):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value != value or value in (float('inf'), float('-inf')) or value <= 0:
            return None
        ts = int(value)
        ms = ts if ts >= 1_000_000_000_000 else ts * 1000
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.isdigit() or (raw.startswith('-') and raw[1:].isdigit()):
            try:
                ts = int(raw)
            except (TypeError, ValueError):
                return None
            if ts <= 0:
                return None
            ms = ts if ts >= 1_000_000_000_000 else ts * 1000
        else:
            try:
                normalized = raw.replace('Z', '+00:00') if raw.endswith('Z') else raw
                dt = datetime.fromisoformat(normalized)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ms = int(dt.timestamp() * 1000)
            except (TypeError, ValueError, OSError, OverflowError):
                return None
    if ms <= 0:
        return None
    try:
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return ms


def _to_iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def normalize_history_series(item: dict[str, Any], source_id: str) -> dict[str, Any] | None:
    """Pass through worker market-series-v1 only. Never invent points from a single price.

    B3.24P: same provider/source only; explicit unavailable always empties points;
    timestamps must parse as UTC; never reuse LKG series as current history.
    """
    history = item.get('history')
    if not isinstance(history, dict):
        return None
    history_source = history.get('sourceId') or source_id
    # Only attach history that matches the selected current value's source.
    if history_source != source_id:
        return {
            'version': 'market-series-v1',
            'status': 'unavailable',
            'interval': '1d',
            'sourceId': source_id,
            'dataAsOf': None,
            'points': [],
            'reason': 'history_source_mismatch',
        }
    # Explicit unavailable — never keep or promote points, never reuse LKG series.
    if history.get('status') == 'unavailable':
        return {
            'version': 'market-series-v1',
            'status': 'unavailable',
            'interval': '1d',
            'sourceId': history_source,
            'dataAsOf': None,
            'points': [],
            'reason': history.get('reason') or 'unavailable',
        }
    raw_points = history.get('points')
    points: list[dict[str, Any]] = []
    if isinstance(raw_points, list):
        seen: dict[str, float] = {}
        order: list[tuple[int, str]] = []
        for point in raw_points:
            if not isinstance(point, dict):
                continue
            stamp = point.get('timestamp')
            value = point.get('value')
            ms = _parse_history_timestamp_ms(stamp)
            if ms is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric != numeric or numeric in (float('inf'), float('-inf')) or numeric <= 0:
                continue
            iso = _to_iso_utc(ms)
            if iso not in seen:
                order.append((ms, iso))
            seen[iso] = numeric  # duplicate timestamps keep last
        order.sort(key=lambda item: item[0])
        # de-dupe order keys while preserving ascending time
        used: set[str] = set()
        points = []
        for _ms, iso in order:
            if iso in used:
                continue
            used.add(iso)
            points.append({'timestamp': iso, 'value': seen[iso]})
    status = history.get('status')
    if status not in {'ok', 'partial', 'unavailable'}:
        status = 'ok' if len(points) >= 2 else ('partial' if len(points) == 1 else 'unavailable')
    if len(points) < 2 and status == 'ok':
        status = 'partial' if points else 'unavailable'
    data_as_of = history.get('dataAsOf') or None
    if data_as_of is not None:
        as_ms = _parse_history_timestamp_ms(data_as_of)
        data_as_of = _to_iso_utc(as_ms) if as_ms is not None else None
    return {
        'version': 'market-series-v1',
        'status': status,
        'interval': '1d',
        'sourceId': history_source,
        'dataAsOf': data_as_of,
        'points': points,
        'reason': history.get('reason'),
    }


def normalize_signal(signal: dict[str, Any], provider: str, item: dict[str, Any], *, status: str = 'ok', reason: str | None = None) -> dict[str, Any]:
    key = signal['key']
    showable = status in {'ok', 'suspect'}
    data_as_of = normalized_as_of(item.get('asOf'))
    age_hours = candidate_age_hours(item)
    source_id = f'{provider}:{item.get("symbol")}'
    out = {
        'key': key,
        'label': signal['label'],
        'value': item.get('price'),
        'change': item.get('change'),
        'changePct': item.get('changePct'),
        'previousClose': item.get('previousClose'),
        'status': 'ok' if status == 'ok' else status,
        'freshnessStatus': 'delayed' if showable else status,
        'provider': provider,
        'sourceId': source_id,
        'fetchedAt': utc_now(),
        'dataAsOf': data_as_of,
        'dataAgeHours': round(age_hours, 2) if age_hours is not None else None,
        'freshnessRank': candidate_freshness_rank(item),
        'ttlMinutes': 30,
        'valuePolicy': 'show' if showable else 'hide',
        'licenseNote': 'free/delayed provider via cache server',
        'coreSignal': key in CORE_SIGNALS,
        'qualityStatus': status,
        'qualityReason': reason,
        'reliability': signal_reliability(key, provider, status, reason, data_as_of),
        'lastSuccessfulAt': utc_now() if showable else None,
    }
    history = normalize_history_series(item, source_id)
    if history is not None:
        out['history'] = history
    return out


def stale_from_last_good(signal: dict[str, Any], last_good: dict[str, Any], reason: str) -> dict[str, Any] | None:
    key = signal['key']
    previous = last_good.get(key)
    if not isinstance(previous, dict) or previous.get('value') in (None, ''):
        return None
    if stale_signal_is_too_old(key, previous):
        return None
    previous_as_of = previous.get('dataAsOf') or previous.get('lastSuccessfulAt') or previous.get('fetchedAt')
    if kr_intraday_stale_reason(key, previous.get('provider') or 'last-known-good', {'asOf': previous_as_of}):
        return None
    previous_quality, previous_reason = validate_candidate(key, {
        'status': 'ok',
        'price': previous.get('value'),
        'changePct': previous.get('changePct'),
        'reason': previous.get('qualityReason'),
    })
    if previous_quality == 'invalid':
        return None
    out = dict(previous)
    out.update({
        'status': 'stale',
        'freshnessStatus': 'stale',
        'valuePolicy': 'show',
        'coreSignal': key in CORE_SIGNALS,
        'qualityStatus': 'stale_last_known_good',
        'qualityReason': reason if previous_quality == 'ok' else previous_reason,
        'reliability': signal_reliability(key, previous.get('provider') or 'free-cache', 'stale', reason),
        'staleSource': 'last_known_good',
        'dataAgeHours': round(stale_signal_age_hours(previous), 2) if stale_signal_age_hours(previous) is not None else None,
        'staleAgeHours': stale_signal_age_hours(previous),
        'maxStaleAgeHours': MAX_STALE_SIGNAL_AGE_HOURS.get(key),
        'fetchedAt': utc_now(),
        'ttlMinutes': previous.get('ttlMinutes') or 360,
        # B3.24: value-only LKG — never promote previous history as current series.
        'history': {
            'version': 'market-series-v1',
            'status': 'unavailable',
            'interval': '1d',
            'sourceId': previous.get('sourceId') or f'last-known-good:{key}',
            'dataAsOf': None,
            'points': [],
            'reason': 'last_known_good_value_only_no_series_promotion',
        },
    })
    return out


def candidate_selection_key(candidate: dict[str, Any]) -> tuple[int, float, int]:
    """Prefer materially fresher displayable data before quality labels.

    A fresh suspect value is still shown with low confidence. This prevents an
    older value labelled ok from silently winning while the freshness audit
    correctly rejects that stale selection.
    """
    age_hours = candidate.get('ageHours')
    age_score = -float(age_hours) if isinstance(age_hours, (int, float)) else -999999.0
    return int(candidate.get('freshnessRank') or 0), age_score, int(candidate.get('qualityRank') or 0)


def choose_signal(signal: dict[str, Any], providers: dict[str, list[dict[str, Any]]], last_good: dict[str, Any]) -> dict[str, Any]:
    key = signal['key']
    failures: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    quality_rank = {'ok': 3, 'suspect': 2, 'partial': 1}
    for provider, items in providers.items():
        for item in items:
            if item.get('key') != key:
                continue
            quality, reason = validate_candidate(key, item)
            stale_reason = (
                kr_intraday_stale_reason(key, provider, item)
                or active_market_stale_reason(key, provider, item)
                or hard_stale_reason(key, provider, item)
            )
            if stale_reason and quality in {'ok', 'suspect', 'partial'}:
                quality, reason = 'stale', stale_reason
            failure = {
                'provider': provider,
                'status': item.get('status'),
                'quality': quality,
                'reason': reason or item.get('reason'),
                'dataAsOf': normalized_as_of(item.get('asOf')) if item.get('asOf') not in (None, '') else None,
                'dataAgeHours': round(candidate_age_hours(item), 2) if candidate_age_hours(item) is not None else None,
                'freshnessRank': candidate_freshness_rank(item),
            }
            failures.append(failure)
            if quality in {'ok', 'suspect', 'partial'}:
                out = normalize_signal(signal, provider, item, status=quality, reason=reason)
                age_hours = candidate_age_hours(item)
                candidates.append({
                    'signal': out,
                    'quality': quality,
                    'qualityRank': quality_rank[quality],
                    'freshnessRank': candidate_freshness_rank(item),
                    'ageHours': age_hours,
                })

    showable_candidates = [item for item in candidates if item['quality'] in {'ok', 'suspect'}]
    if showable_candidates:
        best = max(showable_candidates, key=candidate_selection_key)
        best['signal']['fallbackChain'] = failures
        return best['signal']
    last_good_signal = stale_from_last_good(signal, last_good, failures[-1]['reason'] if failures else 'provider_unavailable')
    if last_good_signal:
        last_good_signal['fallbackChain'] = failures
        return last_good_signal
    partial_candidates = [item for item in candidates if item['quality'] == 'partial']
    if partial_candidates:
        best = max(partial_candidates, key=candidate_selection_key)
        best['signal']['fallbackChain'] = failures
        return best['signal']
    return {
        'key': key,
        'label': signal['label'],
        'value': None,
        'changePct': None,
        'status': 'unavailable' if not failures else 'invalid',
        'freshnessStatus': 'unavailable' if not failures else 'invalid',
        'provider': 'free-provider-poc',
        'sourceId': f'free-provider-poc:{key}',
        'fetchedAt': None,
        'dataAsOf': None,
        'ttlMinutes': 30,
        'valuePolicy': 'neutral_placeholder' if not failures else 'hide',
        'licenseNote': 'free provider key/coverage not verified yet',
        'coreSignal': key in CORE_SIGNALS,
        'qualityStatus': 'unavailable' if not failures else 'invalid',
        'qualityReason': failures[-1]['reason'] if failures else 'provider_unavailable',
        'fallbackChain': failures,
    }


def data_quality(signals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    usable_statuses = {'ok', 'stale', 'suspect'}
    clean_statuses = {'ok'}
    core_ok: list[str] = []
    group_status = {}
    group_details = {}
    for group, keys in CORE_GROUPS.items():
        statuses = [(key, (signals.get(key) or {}).get('status')) for key in keys]
        group_details[group] = {
            key: {
                'status': (signals.get(key) or {}).get('status'),
                'qualityStatus': (signals.get(key) or {}).get('qualityStatus'),
                'qualityReason': (signals.get(key) or {}).get('qualityReason'),
            }
            for key in keys
        }
        usable = [(key, status) for key, status in statuses if status in usable_statuses]
        clean = [(key, status) for key, status in statuses if status in clean_statuses]
        if clean and clean[0][0] == keys[0]:
            group_status[group] = 'ok'
            core_ok.append(clean[0][0])
        elif clean:
            group_status[group] = 'partial_ok'
            core_ok.append(clean[0][0])
        elif usable:
            group_status[group] = usable[0][1] or 'limited'
            core_ok.append(usable[0][0])
        else:
            group_status[group] = 'missing'
    coverage = len([status for status in group_status.values() if status != 'missing']) / max(len(CORE_GROUPS), 1)
    normal_allowed = coverage >= 1.0 and all(status in {'ok', 'partial_ok'} for status in group_status.values())
    return {
        'policy': 'core_signal_fallback_chain_v1',
        'coreSignals': sorted(CORE_SIGNALS),
        'coreGroups': CORE_GROUPS,
        'coreOkSignals': sorted(core_ok),
        'coreCoverageRatio': round(coverage, 3),
        'normalTemperatureAllowed': normal_allowed,
        'displayMode': 'normal' if normal_allowed else ('limited' if coverage >= 0.6 else 'collecting'),
        'groupStatus': group_status,
        'groupDetails': group_details,
        'displayBadge': {
            'kr_index': 'kr_composite_basis' if group_status.get('kr_index') == 'partial_ok' else None,
        },
        'rules': {
            'fallbackOrder': 'primary -> secondary -> current_suspect_with_warning -> last_known_good -> unavailable_state',
            'paidProviderEnabled': False,
            'clientDirectProviderCalls': False,
            'sanityRanges': SANITY_RANGES,
            'fxOfficialProvider': 'bok-ecos-free',
            'volatilityPrimary': ['vix'],
            'volatilitySupplementary': ['vix_aux'],
            'commodityPrimary': ['wti'],
            'commoditySupplementary': ['gold'],
            'vixRateLimitPolicy': 'show_stale_value_with_low_confidence_and_vixy_supplement',
        },
    }


def write_last_known_good(path: Path | None, snapshot: dict[str, Any]) -> None:
    if not path:
        return
    signals = snapshot.get('signals') or {}
    good_signals = {key: value for key, value in signals.items() if isinstance(value, dict) and value.get('status') in {'ok', 'suspect'} and value.get('valuePolicy') == 'show'}
    if not good_signals:
        return
    existing = {'signals': {}}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            existing = {'signals': {}}
    merged = {}
    for key, value in (existing.get('signals') or {}).items():
        if not isinstance(value, dict):
            continue
        quality, _ = validate_candidate(key, {'status': 'ok', 'price': value.get('value'), 'changePct': value.get('changePct')})
        if quality != 'invalid':
            merged[key] = value
    merged.update(good_signals)
    atomic_write_json(path, {'updatedAt': utc_now(), 'signals': merged})


def cached_news(news_probe: dict[str, Any] | None) -> dict[str, Any]:
    if not news_probe:
        return {
            'status': 'unavailable',
            'items': [],
            'sourcePolicy': 'public_rss_headline_cache_only',
            'bodyScrapingEnabled': False,
            'imageScrapingEnabled': False,
        }
    items = news_probe.get('items') if isinstance(news_probe.get('items'), list) else []
    return {
        'status': news_probe.get('status') or ('ok' if items else 'unavailable'),
        'generatedAt': news_probe.get('generatedAt'),
        'ttlMinutes': news_probe.get('ttlMinutes') or 30,
        'nextRefreshAt': news_probe.get('nextRefreshAt'),
        'recommendedSchedule': news_probe.get('recommendedSchedule') or NEWS_RECOMMENDED_SCHEDULE,
        'sourcePolicy': 'public_rss_headline_cache_only',
        'bodyScrapingEnabled': False,
        'imageScrapingEnabled': False,
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'items': [
            {
                'headline': item.get('headline'),
                'displayHeadline': item.get('displayHeadline') or item.get('headline'),
                'originalHeadline': item.get('originalHeadline'),
                'language': item.get('language'),
                'translationNote': item.get('translationNote'),
                'sourceName': item.get('sourceName'),
                'publishedAt': item.get('publishedAt'),
                'url': item.get('url'),
                'impactTarget': item.get('impactTarget') or 'market',
                'impactTone': item.get('impactTone') or 'neutral',
                'category': item.get('category') or 'market_event',
                'categoryLabel': item.get('categoryLabel') or '시장이벤트',
                'tags': item.get('tags') or ['뉴스'],
                'relatedFactors': item.get('relatedFactors') or ['news'],
                'whyImportant': item.get('whyImportant') or '시장 온도와 함께 볼 만한 헤드라인입니다.',
                'scoreAnchor': 'market_temperature_context',
                'qualityScore': item.get('qualityScore'),
                'priorityTier': item.get('priorityTier') or ('CRITICAL' if item.get('critical') else 'STANDARD'),
                'critical': item.get('critical') is True,
                'criticalReason': item.get('criticalReason'),
                'marketImpactScore': item.get('marketImpactScore'),
                'issueClusterKey': item.get('issueClusterKey'),
                'sourceId': item.get('sourceId'),
                'region': item.get('region'),
                'provider': item.get('provider') or 'public-rss',
                'licenseNote': item.get('licenseNote') or 'public RSS headline cache only; no body or image scraping',
            }
            for item in items[:NEWS_SNAPSHOT_MAX_ITEMS]
            if isinstance(item, dict) and item.get('headline') and item.get('url')
        ],
    }


def build_snapshot(probe: dict[str, Any], news_probe: dict[str, Any] | None = None, last_good: dict[str, Any] | None = None) -> dict[str, Any]:
    providers = provider_items(probe)
    signals = {
        signal['key']: choose_signal(signal, providers, last_good or {})
        for signal in probe.get('requiredSignals', [])
    }
    ok_count = sum(1 for item in signals.values() if item['status'] in {'ok', 'stale'})
    quality = data_quality(signals)
    status = 'ok' if quality['normalTemperatureAllowed'] else ('partial' if ok_count else 'needs_keys')
    return {
        'mode': 'free_cache_experiment',
        'generatedAt': utc_now(),
        'status': status,
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'defaultTtlMinutes': 60,
        'probeStatus': probe.get('status'),
        'providerMetrics': provider_metrics(probe),
        'dataQuality': quality,
        'sources': {
            provider['provider']: {
                'status': provider.get('status'),
                'message': provider.get('message'),
            }
            for provider in probe.get('providers', [])
        },
        'signals': signals,
        'macroEvents': build_macro_events(),
        'news': cached_news(news_probe),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', type=Path, default=DEFAULT_PROBE)
    parser.add_argument('--news-probe', type=Path, default=DEFAULT_NEWS_PROBE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--last-known-good', type=Path, default=DEFAULT_LAST_KNOWN_GOOD)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    snapshot = build_snapshot(load_probe(args.probe), load_news_probe(args.news_probe), load_last_known_good(args.last_known_good))
    atomic_write_json(args.output, snapshot)
    write_last_known_good(args.last_known_good, snapshot)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(f"PolarMeter free cache snapshot: wrote {args.output} status={snapshot['status']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
