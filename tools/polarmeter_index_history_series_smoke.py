#!/usr/bin/env python3
"""B3.24P focused smoke: history series pass-through + no LKG series promotion."""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / 'polarmeter_free_provider_probe.py'
SNAPSHOT = HERE / 'polarmeter_cache_snapshot.py'
WORKER = HERE / 'polarmeter_free_cache_worker.py'


def load(path: Path, name: str):
    import sys
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    probe = load(PROBE, 'probe_b324p')
    snap = load(SNAPSHOT, 'snapshot_b324p')
    worker = load(WORKER, 'worker_b324p')

    # reverse + bad values
    points = probe.sanitize_chart_series_points(
        [1_720_172_800, 1_720_000_000, 1_720_086_400],
        [12.0, 10.0, float('nan')],
    )
    assert [p['value'] for p in points] == [10.0, 12.0]

    series = probe.build_market_series_v1(
        timestamps=[1_720_000_000],
        closes=[10.0],
        source_id='public-chart-delayed:SPY',
        data_as_of=None,
        provider_status='ok',
    )
    assert series['status'] == 'partial'
    assert len(series['points']) == 1

    signal = {
        'key': 'sp500',
        'label': 'S&P500/SPY',
        'providerSymbol': {'yahoo_chart': 'SPY'},
        'category': 'us_index',
    }
    item = {
        'key': 'sp500',
        'symbol': 'SPY',
        'price': 500.0,
        'change': 1.0,
        'changePct': 0.2,
        'previousClose': 499.0,
        'asOf': 1_720_172_800,
        'history': {
            'version': 'market-series-v1',
            'status': 'ok',
            'interval': '1d',
            'sourceId': 'public-chart-delayed:SPY',
            'dataAsOf': '2026-07-01T00:00:00Z',
            'points': [
                {'timestamp': '2026-07-02T00:00:00Z', 'value': 11},
                {'timestamp': '2026-07-01T00:00:00Z', 'value': 10},
                {'timestamp': '2026-07-01T00:00:00Z', 'value': 10.5},
                {'timestamp': '2026-07-03T00:00:00Z', 'value': None},
                {'timestamp': '2026-07-04T00:00:00Z', 'value': 0},
            ],
        },
    }
    out = snap.normalize_signal(signal, 'public-chart-delayed', item, status='ok')
    assert out['history']['version'] == 'market-series-v1'
    assert out['previousClose'] == 499.0
    assert [p['timestamp'] for p in out['history']['points']] == [
        '2026-07-01T00:00:00Z',
        '2026-07-02T00:00:00Z',
    ]
    assert out['history']['points'][0]['value'] == 10.5

    # LKG value-only: history must become unavailable empty (no silent reuse)
    from datetime import datetime, timezone, timedelta
    fresh_as_of = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
    last_good = {
        'sp500': {
            **out,
            'dataAsOf': fresh_as_of,
            'lastSuccessfulAt': fresh_as_of,
            'fetchedAt': fresh_as_of,
            'history': {
                'version': 'market-series-v1',
                'status': 'ok',
                'interval': '1d',
                'sourceId': 'public-chart-delayed:SPY',
                'dataAsOf': fresh_as_of,
                'points': [
                    {'timestamp': '2026-06-01T00:00:00Z', 'value': 1},
                    {'timestamp': '2026-07-01T00:00:00Z', 'value': 2},
                ],
            },
        }
    }
    stale = snap.stale_from_last_good(signal, last_good, 'provider_unavailable')
    assert stale is not None, 'fresh LKG value should remain showable as stale'
    assert stale['value'] == 500.0
    assert stale['history']['status'] == 'unavailable'
    assert stale['history']['points'] == []
    assert 'no_series_promotion' in str(stale['history'].get('reason') or '')

    # suspect still normalizes history (not dropped)
    suspect = snap.normalize_signal(signal, 'public-chart-delayed', item, status='suspect', reason='large_move')
    assert suspect['status'] == 'suspect'
    assert len(suspect['history']['points']) >= 2

    # explicit unavailable + valid-looking points → always empty
    unavail_item = {
        **item,
        'history': {
            'version': 'market-series-v1',
            'status': 'unavailable',
            'interval': '1d',
            'sourceId': 'public-chart-delayed:SPY',
            'dataAsOf': '2026-07-01T00:00:00Z',
            'points': [
                {'timestamp': '2026-07-01T00:00:00Z', 'value': 10},
                {'timestamp': '2026-07-02T00:00:00Z', 'value': 11},
            ],
            'reason': 'provider_error',
        },
    }
    unavail_out = snap.normalize_signal(signal, 'public-chart-delayed', unavail_item, status='ok')
    assert unavail_out['history']['status'] == 'unavailable'
    assert unavail_out['history']['points'] == []

    # invalid timestamps dropped; ISO normalized + sorted
    bad_ts_item = {
        **item,
        'history': {
            'version': 'market-series-v1',
            'status': 'ok',
            'interval': '1d',
            'sourceId': 'public-chart-delayed:SPY',
            'dataAsOf': 'not-a-date',
            'points': [
                {'timestamp': 'not-a-date', 'value': 9},
                {'timestamp': '2026-07-03T00:00:00Z', 'value': 12},
                {'timestamp': '2026-07-01T00:00:00Z', 'value': 10},
                {'timestamp': '', 'value': 11},
                {'timestamp': 0, 'value': 8},
            ],
        },
    }
    bad_out = snap.normalize_signal(signal, 'public-chart-delayed', bad_ts_item, status='ok')
    assert [p['timestamp'] for p in bad_out['history']['points']] == [
        '2026-07-01T00:00:00Z',
        '2026-07-03T00:00:00Z',
    ]
    assert bad_out['history']['dataAsOf'] is None

    # source mismatch → empty history
    mismatch_item = {
        **item,
        'history': {
            'version': 'market-series-v1',
            'status': 'ok',
            'interval': '1d',
            'sourceId': 'other-provider:SPY',
            'dataAsOf': '2026-07-01T00:00:00Z',
            'points': [
                {'timestamp': '2026-07-01T00:00:00Z', 'value': 10},
                {'timestamp': '2026-07-02T00:00:00Z', 'value': 11},
            ],
        },
    }
    mismatch_out = snap.normalize_signal(signal, 'public-chart-delayed', mismatch_item, status='ok')
    assert mismatch_out['history']['status'] == 'unavailable'
    assert mismatch_out['history']['points'] == []
    assert mismatch_out['history']['reason'] == 'history_source_mismatch'

    # probe: unavailable provider_status empties points even if arrays present
    blocked = probe.build_market_series_v1(
        timestamps=[1_720_000_000, 1_720_086_400],
        closes=[10.0, 11.0],
        source_id='public-chart-delayed:SPY',
        data_as_of=None,
        provider_status='unavailable',
        reason='blocked',
    )
    assert blocked['status'] == 'unavailable'
    assert blocked['points'] == []

    # probe rejects garbage timestamps
    cleaned = probe.sanitize_chart_series_points(
        ['not-a-date', 1_720_000_000, -5, 0, None],
        [1.0, 10.0, 2.0, 3.0, 4.0],
    )
    assert len(cleaned) == 1
    assert cleaned[0]['value'] == 10.0
    assert cleaned[0]['timestamp'].endswith('Z')

    # public worker must keep sanitized history (not strip it)
    public = worker.sanitize_public_signal(out)
    assert 'history' in public
    assert public['history']['version'] == 'market-series-v1'
    assert len(public['history']['points']) == 2
    assert public.get('previousClose') == 499.0
    public_unavail = worker.sanitize_public_signal(unavail_out)
    assert public_unavail['history']['points'] == []
    assert public_unavail['history']['status'] == 'unavailable'

    # B3.24P2: public-edge timestamp safety filter (parse/sort/dedupe/normalize)
    raw_public_hist = {
        'version': 'market-series-v1',
        'status': 'ok',
        'interval': '1w',  # must be forced to 1d
        'sourceId': 'public-chart-delayed:SPY',
        'dataAsOf': 'not-a-date',
        'points': [
            {'timestamp': 'not-a-date', 'value': 9},
            {'timestamp': '2026-02-30T00:00:00Z', 'value': 8},  # invalid calendar
            {'timestamp': '2026-07-03T00:00:00Z', 'value': 12},
            {'timestamp': '2026-07-01T00:00:00Z', 'value': 10},
            {'timestamp': '2026-07-01T00:00:00Z', 'value': 10.5},  # dup keep last
            {'timestamp': '', 'value': 11},
            {'timestamp': 0, 'value': 8},
            {'timestamp': '2026-07-02T00:00:00Z', 'value': 'not-a-number'},
            {'timestamp': '2026-07-04T12:34:56Z', 'value': 13},
        ],
    }
    pub_h = worker.sanitize_public_history(raw_public_hist)
    assert pub_h is not None
    assert pub_h['interval'] == '1d'
    assert pub_h['dataAsOf'] is None
    assert [p['timestamp'] for p in pub_h['points']] == [
        '2026-07-01T00:00:00Z',
        '2026-07-03T00:00:00Z',
        '2026-07-04T12:34:56Z',
    ]
    assert pub_h['points'][0]['value'] == 10.5
    assert all(p['timestamp'].endswith('Z') for p in pub_h['points'])
    # reverse order input still sorted ascending
    assert pub_h['points'][0]['timestamp'] < pub_h['points'][1]['timestamp'] < pub_h['points'][2]['timestamp']

    # dataAsOf parseable → ISO UTC
    pub_asof = worker.sanitize_public_history({
        **raw_public_hist,
        'dataAsOf': '2026-07-04T12:34:56+00:00',
        'points': [{'timestamp': '2026-07-01T00:00:00Z', 'value': 10}],
    })
    assert pub_asof['dataAsOf'] == '2026-07-04T12:34:56Z'

    # empty / non-string sourceId → unavailable empty
    for bad_sid in ('', '   ', None, 123, []):
        bad_src = worker.sanitize_public_history({
            'version': 'market-series-v1',
            'status': 'ok',
            'interval': '1d',
            'sourceId': bad_sid,
            'dataAsOf': '2026-07-01T00:00:00Z',
            'points': [{'timestamp': '2026-07-01T00:00:00Z', 'value': 10}],
        })
        assert bad_src['status'] == 'unavailable'
        assert bad_src['points'] == []
        assert bad_src['interval'] == '1d'

    # explicit unavailable + valid-looking points → still empty at public edge
    pub_unavail_direct = worker.sanitize_public_history({
        'version': 'market-series-v1',
        'status': 'unavailable',
        'interval': '1d',
        'sourceId': 'public-chart-delayed:SPY',
        'dataAsOf': '2026-07-01T00:00:00Z',
        'points': [
            {'timestamp': '2026-07-01T00:00:00Z', 'value': 10},
            {'timestamp': '2026-07-02T00:00:00Z', 'value': 11},
        ],
        'reason': 'provider_error',
    })
    assert pub_unavail_direct['status'] == 'unavailable'
    assert pub_unavail_direct['points'] == []
    assert pub_unavail_direct['interval'] == '1d'

    print('PolarMeter index history series smoke (B3.24P2): PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
