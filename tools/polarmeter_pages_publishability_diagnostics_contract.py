#!/usr/bin/env python3
"""Offline contract for safe, local-only GitHub Pages publish diagnostics."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polarmeter_github_pages_prepare as prepare


# Inert fixture text, not a credential: arbitrary input must never reach logs.
UNTRUSTED_SENTINEL = 'UNTRUSTED_PAYLOAD_MUST_NOT_APPEAR'


def write_payload(output_dir: Path, health: dict, snapshot: dict, manifest: dict) -> None:
    (output_dir / 'health.json').write_text(json.dumps(health), encoding='utf-8')
    (output_dir / 'market-snapshot-latest.json').write_text(json.dumps(snapshot), encoding='utf-8')
    (output_dir / 'market-snapshot-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')


def valid_payload() -> tuple[dict, dict, dict]:
    return (
        {'ok': True},
        {
            'dataQuality': {'coreCoverageRatio': 0.8, 'displayMode': 'normal'},
            'news': {'items': [{} for _ in range(10)]},
            'temperatureHistory': {
                'version': 'temperature-history-v1',
                'retentionDays': 7,
                'items': [],
                'dailyDelta': {'status': 'ready'},
            },
        },
        {'okNewsCount': 10},
    )


def main() -> None:
    # This guards the large-int path without converting an arbitrary integer to float.
    huge_integer = 10 ** 10000
    assert prepare._safe_number(huge_integer) == huge_integer
    assert prepare._safe_number(float('nan')) is None
    assert prepare._safe_number(float('inf')) is None

    with TemporaryDirectory(prefix='polarmeter-pages-diagnostics-') as temp:
        output_dir = Path(temp)
        health, snapshot, manifest = valid_payload()
        write_payload(output_dir, health, snapshot, manifest)
        assert prepare.public_payload_publishability_failures(output_dir) == []
        assert prepare.public_payload_is_publishable(output_dir) is True

        mutations = [
            ('health.ok', lambda h, s, m: h.update(ok=False)),
            ('dataQuality.coreCoverageRatio', lambda h, s, m: s['dataQuality'].update(coreCoverageRatio=float('nan'))),
            ('dataQuality.displayMode', lambda h, s, m: s['dataQuality'].update(displayMode='collecting')),
            ('snapshot.news.items', lambda h, s, m: s['news'].update(items=[])),
            ('manifest.okNewsCount', lambda h, s, m: m.update(okNewsCount=0)),
            ('temperatureHistory.version', lambda h, s, m: s['temperatureHistory'].update(version=UNTRUSTED_SENTINEL)),
            ('temperatureHistory.retentionDays', lambda h, s, m: s['temperatureHistory'].update(retentionDays=6)),
            ('temperatureHistory.items', lambda h, s, m: s['temperatureHistory'].update(items={'private': UNTRUSTED_SENTINEL})),
            ('temperatureHistory.dailyDelta.status', lambda h, s, m: s['temperatureHistory'].update(dailyDelta={'status': 'pending'})),
        ]
        for check, mutate in mutations:
            h, s, m = json.loads(json.dumps(health)), json.loads(json.dumps(snapshot)), json.loads(json.dumps(manifest))
            mutate(h, s, m)
            write_payload(output_dir, h, s, m)
            failures = prepare.public_payload_publishability_failures(output_dir)
            assert check in {failure['check'] for failure in failures}
            assert prepare.public_payload_is_publishable(output_dir) is False

            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                prepare.emit_public_payload_diagnostics(output_dir)
            emitted = captured.getvalue()
            assert emitted.startswith('{"pagesPublishabilityFailures":')
            assert UNTRUSTED_SENTINEL not in emitted
            assert 'PRIVATE_EVALUATION_NOTE' not in emitted
            assert 'pagesPublishabilityFailures' not in json.dumps(s)

        # Malformed files get one bounded reason and never echo their contents.
        (output_dir / 'health.json').write_text(UNTRUSTED_SENTINEL, encoding='utf-8')
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            prepare.emit_public_payload_diagnostics(output_dir)
        assert 'unreadable' in captured.getvalue()
        assert UNTRUSTED_SENTINEL not in captured.getvalue()

    news_shortfall = [
        {'check': 'snapshot.news.items', 'reason': 'below_minimum'},
        {'check': 'manifest.okNewsCount', 'reason': 'below_minimum'},
    ]
    assert prepare.transient_news_shortfall_only(news_shortfall)
    assert not prepare.transient_news_shortfall_only([
        {'check': 'health.ok', 'reason': 'value_mismatch'},
    ])
    with (
        patch.object(prepare, 'run_worker', side_effect=[{'attempt': 1}, {'attempt': 2}]) as worker,
        patch.object(prepare, 'public_payload_publishability_failures', side_effect=[news_shortfall, []]),
        patch.object(prepare.time, 'sleep') as sleeper,
    ):
        summary, failures = prepare.run_worker_with_publishability_retries(
            Path('output'),
            Path('last-good'),
            news_shortfall_retries=1,
            retry_delay_seconds=45,
        )
    assert summary == {'attempt': 2} and failures == []
    assert worker.call_count == 2
    sleeper.assert_called_once_with(45)

    with (
        patch.object(prepare, 'run_worker', return_value={'attempt': 1}) as worker,
        patch.object(prepare, 'public_payload_publishability_failures', return_value=[
            {'check': 'health.ok', 'reason': 'value_mismatch'},
        ]),
        patch.object(prepare.time, 'sleep') as sleeper,
    ):
        _, failures = prepare.run_worker_with_publishability_retries(
            Path('output'),
            Path('last-good'),
            news_shortfall_retries=1,
            retry_delay_seconds=45,
        )
    assert failures[0]['check'] == 'health.ok'
    assert worker.call_count == 1
    sleeper.assert_not_called()

    print('PASS Pages publishability diagnostics remain local and safe')


if __name__ == '__main__':
    main()
