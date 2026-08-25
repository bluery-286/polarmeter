#!/usr/bin/env python3
"""Offline security checks for the public PolarMeter repository and Pages payload."""
from __future__ import annotations

import argparse
import re
import urllib.error
from pathlib import Path

from polarmeter_free_cache_worker import assert_public_payload_safe
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
    parser.add_argument('--public-dir', type=Path, default=ROOT / 'github-pages-site')
    args = parser.parse_args()
    assert_error_redaction()
    assert_news_url_policy()
    assert_action_pinning()
    assert_single_pages_publisher()
    assert_public_payload_clean(args.public_dir)
    assert_secret_files_ignored()
    assert_public_payload_secret_guard()
    print('PolarMeter security contract: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
