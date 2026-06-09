#!/usr/bin/env python3
"""Build a provider-neutral free-cache snapshot for PolarMeter.

The snapshot is the cache-server contract. It can be built from a provider probe
report or, while keys are unavailable, as a safe placeholder that makes missing
coverage explicit without calling paid providers.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
PROJECT = WORKSPACE
DEFAULT_PROBE = PROJECT / 'testflight/free-provider-probe-report-latest.json'
DEFAULT_OUTPUT = PROJECT / 'testflight/free-cache-snapshot-latest.json'
DEFAULT_NEWS_PROBE = PROJECT / 'testflight/news-rss-probe-latest.json'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_probe(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def load_news_probe(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def provider_items(probe: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {provider.get('provider', 'unknown'): provider.get('items', []) for provider in probe.get('providers', [])}


def choose_signal(signal: dict[str, Any], providers: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    key = signal['key']
    for provider, items in providers.items():
        for item in items:
            if item.get('key') == key and item.get('status') == 'ok':
                return {
                    'key': key,
                    'label': signal['label'],
                    'value': item.get('price'),
                    'change': item.get('change'),
                    'changePct': item.get('changePct'),
                    'status': 'ok',
                    'freshnessStatus': 'delayed',
                    'provider': provider,
                    'sourceId': f'{provider}:{item.get("symbol")}',
                    'fetchedAt': utc_now(),
                    'dataAsOf': item.get('asOf') or utc_now(),
                    'ttlMinutes': 30,
                    'valuePolicy': 'show',
                    'licenseNote': 'free/delayed provider via cache server',
                }
    return {
        'key': key,
        'label': signal['label'],
        'value': None,
        'changePct': None,
        'status': 'unavailable',
        'freshnessStatus': 'unavailable',
        'provider': 'free-provider-poc',
        'sourceId': f'free-provider-poc:{key}',
        'fetchedAt': None,
        'dataAsOf': None,
        'ttlMinutes': 30,
        'valuePolicy': 'neutral_placeholder',
        'licenseNote': 'free provider key/coverage not verified yet',
    }


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
        'sourcePolicy': 'public_rss_headline_cache_only',
        'bodyScrapingEnabled': False,
        'imageScrapingEnabled': False,
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'items': [
            {
                'headline': item.get('headline'),
                'sourceName': item.get('sourceName'),
                'publishedAt': item.get('publishedAt'),
                'url': item.get('url'),
                'impactTarget': item.get('impactTarget') or 'market',
                'impactTone': item.get('impactTone') or 'neutral',
                'tags': item.get('tags') or ['뉴스'],
                'sourceId': item.get('sourceId'),
                'provider': item.get('provider') or 'public-rss',
                'licenseNote': item.get('licenseNote') or 'public RSS headline cache only; no body or image scraping',
            }
            for item in items[:6]
            if isinstance(item, dict) and item.get('headline') and item.get('url')
        ],
    }


def build_snapshot(probe: dict[str, Any], news_probe: dict[str, Any] | None = None) -> dict[str, Any]:
    providers = provider_items(probe)
    signals = {
        signal['key']: choose_signal(signal, providers)
        for signal in probe.get('requiredSignals', [])
    }
    ok_count = sum(1 for item in signals.values() if item['status'] == 'ok')
    status = 'ok' if ok_count == len(signals) and signals else ('partial' if ok_count else 'needs_keys')
    return {
        'mode': 'free_cache_experiment',
        'generatedAt': utc_now(),
        'status': status,
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'defaultTtlMinutes': 30,
        'probeStatus': probe.get('status'),
        'sources': {
            provider['provider']: {
                'status': provider.get('status'),
                'message': provider.get('message'),
            }
            for provider in probe.get('providers', [])
        },
        'signals': signals,
        'news': cached_news(news_probe),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', type=Path, default=DEFAULT_PROBE)
    parser.add_argument('--news-probe', type=Path, default=DEFAULT_NEWS_PROBE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    snapshot = build_snapshot(load_probe(args.probe), load_news_probe(args.news_probe))
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(f"PolarMeter free cache snapshot: wrote {args.output} status={snapshot['status']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
