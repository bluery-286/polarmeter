#!/usr/bin/env python3
"""Regression contract for official PolarMeter macro release rollover."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import polarmeter_cache_snapshot as snapshot
from polarmeter_github_pages_smoke import validate_cpi_rollover


SYNTHETIC_FOMC_HTML = """
<html><body>
<p>The Federal Open Market Committee approved the following statement for release by a 9 – 3 vote:</p>
<p>The Committee decided to maintain the target range for the federal funds rate at
3-1/2 to 3-3/4 percent.</p>
<p>Voting against the monetary policy action were three members, who preferred to
raise the target range for the federal funds rate by 1/4 percentage point at this meeting.</p>
</body></html>
"""

SYNTHETIC_BLS_PAYLOAD = {
    'status': 'REQUEST_SUCCEEDED',
    'message': [],
    'Results': {
        'series': [
            {
                'seriesID': 'CUSR0000SA0',
                'data': [
                    {'year': '2026', 'period': 'M07', 'value': '332.568'},
                    {'year': '2026', 'period': 'M06', 'value': '333.979'},
                ],
            },
            {
                'seriesID': 'CUUR0000SA0',
                'data': [
                    {'year': '2026', 'period': 'M07', 'value': '333.952'},
                    {'year': '2025', 'period': 'M07', 'value': '322.560'},
                ],
            },
            {
                'seriesID': 'CES0000000001',
                'data': [
                    {'year': '2026', 'period': 'M07', 'value': '159041'},
                    {'year': '2026', 'period': 'M06', 'value': '158984'},
                ],
            },
            {
                'seriesID': 'LNS14000000',
                'data': [
                    {'year': '2026', 'period': 'M07', 'value': '4.2'},
                ],
            },
        ],
    },
}


def raising_fetcher(error: BaseException):
    def fetcher(_series, _start, _end):
        raise error

    return fetcher


def assert_raises_value_error(fetcher, message: str) -> None:
    try:
        snapshot.build_macro_events(
            now=datetime(2026, 8, 12, 14, 31, tzinfo=timezone.utc),
            bls_fetcher=fetcher,
        )
    except ValueError:
        return
    raise AssertionError(message)


def main() -> None:
    parsed = snapshot.parse_fomc_statement(
        SYNTHETIC_FOMC_HTML,
        label='9월 FOMC',
        released_at=datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc),
        source_url='https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm',
    )
    assert parsed['resultLabel'] == '기준금리 3.50~3.75% 동결'
    assert '9대3' in parsed['detail']
    assert '0.25%포인트 인상' in parsed['detail']

    current = snapshot.build_macro_events(now=datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc))
    assert current['fomc_rate']['lastRelease']['label'] == '7월 FOMC'
    assert current['fomc_rate']['nextRelease']['label'] == '9월 FOMC'

    with patch.dict(
        snapshot.SCHEDULED_MACRO_EVENTS,
        {'fomc_rate': snapshot.SCHEDULED_MACRO_EVENTS['fomc_rate']},
        clear=True,
    ):
        future = snapshot.build_macro_events(
            now=datetime(2026, 9, 16, 18, 10, tzinfo=timezone.utc),
            fomc_fetcher=lambda _url: SYNTHETIC_FOMC_HTML,
        )
    assert future['fomc_rate']['status'] == 'ok'
    assert future['fomc_rate']['lastRelease']['label'] == '9월 FOMC'
    assert future['fomc_rate']['nextRelease']['label'] == '10월 FOMC'

    cpi = snapshot.parse_bls_cpi_release(
        SYNTHETIC_BLS_PAYLOAD,
        label='7월 CPI',
        released_at=datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
        source_url='https://www.bls.gov/news.release/cpi.nr0.htm',
    )
    assert cpi['resultLabel'] == '물가 전월 대비 -0.4% · 전년 대비 +3.5%'
    assert isinstance(cpi['burdenScore'], int)

    jobs = snapshot.parse_bls_employment_release(
        SYNTHETIC_BLS_PAYLOAD,
        label='7월 고용',
        released_at=datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc),
        source_url='https://www.bls.gov/news.release/empsit.nr0.htm',
    )
    assert jobs['resultLabel'] == '일자리 +5.7만명 · 실업률 4.2%'

    for key, now, expected_label in [
        ('us_nonfarm_payrolls', datetime(2026, 8, 7, 12, 40, tzinfo=timezone.utc), '7월 고용'),
        ('us_cpi', datetime(2026, 8, 12, 12, 40, tzinfo=timezone.utc), '7월 CPI'),
    ]:
        with patch.dict(
            snapshot.SCHEDULED_MACRO_EVENTS,
            {key: snapshot.SCHEDULED_MACRO_EVENTS[key]},
            clear=True,
        ):
            rolled = snapshot.build_macro_events(
                now=now,
                bls_fetcher=lambda _series, _start, _end: SYNTHETIC_BLS_PAYLOAD,
            )
        assert rolled[key]['status'] == 'ok'
        assert rolled[key]['lastRelease']['label'] == expected_label

    with patch.dict(
        snapshot.SCHEDULED_MACRO_EVENTS,
        {'us_cpi': snapshot.SCHEDULED_MACRO_EVENTS['us_cpi']},
        clear=True,
    ):
        awaiting = snapshot.build_macro_events(
            now=datetime(2026, 8, 12, 12, 40, tzinfo=timezone.utc),
            bls_fetcher=lambda _series, _start, _end: {
                'status': 'REQUEST_SUCCEEDED',
                'message': [],
                'Results': {'series': []},
            },
        )
        assert awaiting['us_cpi']['status'] == 'awaiting_official'
        assert_raises_value_error(
            lambda _series, _start, _end: {
                'status': 'REQUEST_SUCCEEDED',
                'message': [],
                'Results': {'series': []},
            },
            'missing official CPI result remained silently successful after two hours',
        )
        assert_raises_value_error(
            raising_fetcher(ValueError('invalid official BLS response')),
            'invalid official BLS response remained silently successful after two hours',
        )

        for transient_error in [
            HTTPError(snapshot.BLS_API_URL, 429, 'Too Many Requests', None, None),
            HTTPError(snapshot.BLS_API_URL, 503, 'Service Unavailable', None, None),
            URLError('planned maintenance'),
            TimeoutError('official BLS request timed out'),
        ]:
            delayed = snapshot.build_macro_events(
                now=datetime(2026, 8, 12, 14, 31, tzinfo=timezone.utc),
                bls_fetcher=raising_fetcher(transient_error),
            )
            assert delayed['us_cpi']['status'] == 'awaiting_official'
            assert delayed['us_cpi']['lastRelease']['label'] == '6월 CPI'
            assert delayed['us_cpi']['nextRelease']['label'] == '7월 CPI'
            validate_cpi_rollover({
                'generatedAt': '2026-08-12T14:31:00Z',
                'macroEvents': delayed,
            })

        try:
            snapshot.build_macro_events(
                now=datetime(2026, 8, 12, 14, 31, tzinfo=timezone.utc),
                bls_fetcher=raising_fetcher(
                    HTTPError(snapshot.BLS_API_URL, 404, 'Not Found', None, None),
                ),
            )
        except HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError('HTTP 404 remained silently successful after two hours')

    assert snapshot.SCHEDULED_MACRO_EVENTS['fomc_rate'][-1] == (
        '2027-12-08T19:00:00Z',
        '12월 FOMC',
    )
    print('PASS — official FOMC, CPI, and employment release rollover')


if __name__ == '__main__':
    main()
