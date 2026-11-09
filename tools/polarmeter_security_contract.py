#!/usr/bin/env python3
"""Offline security checks for the public PolarMeter repository and Pages payload."""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
from pathlib import Path

from polarmeter_coach_brief import assert_public_coach_brief, assert_public_coach_brief_if_present
from polarmeter_coach_brief_contract import sample_brief
from polarmeter_evening_brief import assert_public_evening_brief, assert_public_evening_if_present
from polarmeter_evening_brief_contract import sample_evening_block
from polarmeter_free_cache_worker import assert_public_payload_safe, sanitize_public_snapshot
from polarmeter_morning_forecast import assert_public_forecast_if_present, assert_public_morning_forecast
from polarmeter_morning_forecast_contract import sample_block
from polarmeter_push_outbox import assert_public_push_outbox, assert_public_push_outbox_if_present
from polarmeter_push_outbox_contract import sample_outbox
from polarmeter_free_provider_probe import safe_network_error
from polarmeter_news_rss_probe import safe_public_news_url


ROOT = Path(__file__).resolve().parents[1]
PINNED_ACTION = re.compile(r'^\s*-?\s*uses:\s*(actions|github)/[^@\s]+@[0-9a-f]{40}(?:\s+#.*)?$', re.I)
ANY_ACTION = re.compile(r'^\s*-?\s*uses:\s*[^\s#]+', re.I)
PUBLIC_DATA_SECRET_MARKERS = (
    'TWELVE_DATA_API_KEY',
    'FMP_API_KEY',
    'DATA_GO_KR_SERVICE_KEY',
    'apikey=',
    'serviceKey=',
)
PUBLIC_ALWAYS_FORBIDDEN = (
    '-----BEGIN ' + 'PRIVATE KEY-----',
    '/Users/',
    'C:\\Users\\',
    '/home/runner/work/',
)


def assert_error_redaction() -> None:
    secret_url = 'https://api.example.test/quote?apikey=do-not-publish'
    http_error = urllib.error.HTTPError(secret_url, 429, 'rate limited', {}, None)
    reason = safe_network_error(http_error)
    assert reason == 'HTTPError:429'
    assert 'do-not-publish' not in reason and secret_url not in reason

    url_error = urllib.error.URLError(f'connection failed for {secret_url}')
    reason = safe_network_error(url_error)
    assert reason == 'URLError:str'
    assert 'do-not-publish' not in reason and secret_url not in reason


def assert_news_url_policy() -> None:
    assert safe_public_news_url('https://news.example.test/article?id=1')
    for unsafe in (
        'http://news.example.test/article',
        'javascript:alert(1)',
        'data:text/html,unsafe',
        'https://user' + ':password@news.example.test/article',
        'https:///missing-host',
        'https://news.example.test/article\nheader: injected',
        'https://localhost/private',
        'https://127.0.0.1/private',
        'https://10.0.0.1/private',
        'https://[::1]/private',
    ):
        assert safe_public_news_url(unsafe) is None, unsafe


def assert_action_pinning() -> None:
    workflow_dir = ROOT / '.github' / 'workflows'
    for workflow in sorted(workflow_dir.glob('*.y*ml')):
        for line_number, line in enumerate(workflow.read_text(encoding='utf-8').splitlines(), 1):
            if ANY_ACTION.match(line):
                assert PINNED_ACTION.match(line), f'unpinned or unapproved action: {workflow}:{line_number}'


def assert_single_pages_publisher() -> None:
    workflow_dir = ROOT / '.github' / 'workflows'
    cache_workflow = (workflow_dir / 'polarmeter-cache-pages.yml').read_text(encoding='utf-8')
    static_workflow = (workflow_dir / 'polarmeter-static-pages.yml').read_text(encoding='utf-8')
    assert 'git push --force origin HEAD:gh-pages' in cache_workflow
    assert 'deploy-pages' not in static_workflow
    assert not re.search(r'^\s*pages:\s*write\s*$', static_workflow, re.M)
    assert 'without publishing' in static_workflow


def assert_public_payload_clean(public_dir: Path) -> None:
    assert public_dir.is_dir(), f'public payload directory is missing: {public_dir}'
    for path in sorted(public_dir.rglob('*')):
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        markers = PUBLIC_ALWAYS_FORBIDDEN + (PUBLIC_DATA_SECRET_MARKERS if path.suffix == '.json' else ())
        leaked = [marker for marker in markers if marker.lower() in text.lower()]
        assert not leaked, f'public file contains forbidden marker(s): {path}: {leaked}'


def assert_morning_forecast_public() -> None:
    """The forecast block passes the public-payload guard, survives the sanitizer unchanged and keeps its shape."""
    block = sample_block()
    assert_public_morning_forecast(block)
    assert_public_payload_safe({'morningForecast': block})
    published = sanitize_public_snapshot({'generatedAt': '2026-11-05T21:45:00Z', 'morningForecast': block})
    assert published['morningForecast'] == block, 'sanitize_public_snapshot must pass morningForecast through unchanged'
    assert sanitize_public_snapshot({'generatedAt': '2026-11-05T21:45:00Z'})['morningForecast'] is None
    for leaked in ({**block, 'apiKey': 'do-not-publish'}, {**block, 'ledger': [{**block['ledger'][0], 'secretNote': 'x'}]}):
        try:
            assert_public_payload_safe({'morningForecast': leaked})
        except AssertionError:
            continue
        raise AssertionError('public payload guard accepted a leaking morningForecast')


def assert_coach_brief_public() -> None:
    """The coach brief passes the public-payload guard, survives the sanitizer unchanged and keeps its shape."""
    brief = sample_brief()
    assert_public_coach_brief(brief)
    assert_public_payload_safe({'coachBrief': brief})
    published = sanitize_public_snapshot({'generatedAt': '2026-11-05T21:50:00Z', 'coachBrief': brief})
    assert published['coachBrief'] == brief, 'sanitize_public_snapshot must pass coachBrief through unchanged'
    assert sanitize_public_snapshot({'generatedAt': '2026-11-05T21:50:00Z'})['coachBrief'] is None
    try:
        assert_public_payload_safe({'coachBrief': {**brief, 'apiKey': 'do-not-publish'}})
    except AssertionError:
        return
    raise AssertionError('public payload guard accepted a leaking coachBrief')


def assert_push_outbox_public() -> None:
    outbox = sample_outbox()
    assert outbox['items'], 'the sample push outbox must carry an item'
    assert_public_push_outbox(outbox)
    assert_public_payload_safe({'pushOutbox': outbox})
    published = sanitize_public_snapshot({'generatedAt': '2026-11-05T21:55:00Z', 'pushOutbox': outbox})
    assert published['pushOutbox'] == outbox, 'sanitize_public_snapshot must pass pushOutbox through unchanged'
    assert sanitize_public_snapshot({'generatedAt': '2026-11-05T21:55:00Z'})['pushOutbox'] is None
    try:
        assert_public_payload_safe({'pushOutbox': {**outbox, 'apiKey': 'do-not-publish'}})
    except AssertionError:
        return
    raise AssertionError('public payload guard accepted a leaking pushOutbox')


def assert_evening_brief_public() -> None:
    block = sample_evening_block()
    assert block['current'], 'the sample evening brief must carry tonight'
    assert_public_evening_brief(block)
    assert_public_payload_safe({'eveningBrief': block})
    published = sanitize_public_snapshot({'generatedAt': '2026-11-10T12:15:00Z', 'eveningBrief': block})
    assert published['eveningBrief'] == block, 'sanitize_public_snapshot must pass eveningBrief through unchanged'
    assert sanitize_public_snapshot({'generatedAt': '2026-11-10T12:15:00Z'})['eveningBrief'] is None
    try:
        assert_public_payload_safe({'eveningBrief': {**block, 'apiKey': 'do-not-publish'}})
    except AssertionError:
        return
    raise AssertionError('public payload guard accepted a leaking eveningBrief')


def assert_public_forecast_in_payload(public_dir: Path) -> None:
    """A public snapshot that carries a morningForecast must carry a well-formed one. A snapshot without it (old data,
    the rollback path) passes: the forecast layer must never block a publish."""
    snapshot_path = public_dir / 'market-snapshot-latest.json'
    if snapshot_path.is_file():
        snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
        assert_public_forecast_if_present(snapshot)
        assert_public_coach_brief_if_present(snapshot)  # the same rule for the coach brief: absent or null passes
        assert_public_push_outbox_if_present(snapshot)  # and for the push outbox
        assert_public_evening_if_present(snapshot)  # and for the evening brief


def assert_secret_files_ignored() -> None:
    patterns = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    required = {'.env', '.env.*', '*.pem', '*.key', '*.p12', '*.jks', 'google-services.json', 'GoogleService-Info.plist'}
    assert not required.difference(patterns), f'missing secret ignore patterns: {sorted(required.difference(patterns))}'


def assert_public_payload_secret_guard() -> None:
    # Normal public reporting words must not be mistaken for credentials.
    assert_public_payload_safe({
        'news': {
            'items': [{
                'headline': 'Global food supply discussed by the Secretary-General',
                'displayHeadline': 'Treasury secretary comments on markets',
                'url': 'https://news.example.test/treasury-secretary',
            }],
        },
    })

    blocked_payloads = (
        {'apiKey': 'do-not-publish'},
        {'clientSecret': 'do-not-publish'},
        {'feedResults': []},
        {'news': {'items': [{'url': 'https://api.example.test/quote?apikey=do-not-publish'}]}},
        {'status': 'missing_key'},
    )
    for payload in blocked_payloads:
        try:
            assert_public_payload_safe(payload)
        except AssertionError:
            continue
        raise AssertionError(f'public payload secret guard accepted unsafe data: {payload}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--public-dir', type=Path, default=None)
    args = parser.parse_args()
    args.public_dir = args.public_dir or ROOT / 'github-pages-site'
    assert_error_redaction()
    assert_news_url_policy()
    assert_action_pinning()
    assert_single_pages_publisher()
    assert_public_payload_clean(args.public_dir)
    assert_public_forecast_in_payload(args.public_dir)
    assert_secret_files_ignored()
    assert_public_payload_secret_guard()
    assert_morning_forecast_public()
    assert_coach_brief_public()
    assert_push_outbox_public()
    assert_evening_brief_public()
    print('PolarMeter security contract: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
