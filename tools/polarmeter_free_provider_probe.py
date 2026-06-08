#!/usr/bin/env python3
"""Probe free/delayed data-provider coverage for PolarMeter.

This tool is safe-by-default: it only calls providers when an API key is present
in the environment. Without keys, it emits a structured missing-key report so the
POC can proceed without pretending coverage is verified.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECRETS_DIR = Path.home() / '.openclaw/secrets'

REQUIRED_SIGNALS = [
    {'key': 'sp500', 'label': 'S&P500/SPY', 'providerSymbol': {'twelvedata': 'SPY', 'fmp': 'SPY'}, 'category': 'us_index'},
    {'key': 'nasdaq100', 'label': 'Nasdaq100/QQQ', 'providerSymbol': {'twelvedata': 'QQQ', 'fmp': 'QQQ'}, 'category': 'us_index'},
    {'key': 'vix', 'label': 'VIX proxy', 'providerSymbol': {'twelvedata': 'VIXY', 'fmp': 'VIXY'}, 'category': 'volatility'},
    {'key': 'usd_krw', 'label': 'USD/KRW', 'providerSymbol': {'twelvedata': 'USD/KRW', 'fmp': 'USD/KRW'}, 'category': 'fx'},
    {'key': 'wti', 'label': 'WTI proxy', 'providerSymbol': {'twelvedata': 'CL', 'fmp': 'CLUSD'}, 'category': 'commodity'},
    {'key': 'soxx', 'label': 'SOXX', 'providerSymbol': {'twelvedata': 'SOXX', 'fmp': 'SOXX'}, 'category': 'semiconductor'},
    {'key': 'smh', 'label': 'SMH', 'providerSymbol': {'twelvedata': 'SMH', 'fmp': 'SMH'}, 'category': 'semiconductor'},
    {'key': 'kr_samsung', 'label': '삼성전자', 'providerSymbol': {'data_go_kr': '005930'}, 'category': 'kr_proxy'},
    {'key': 'kospi', 'label': 'KOSPI', 'providerSymbol': {'data_go_kr_index': '코스피'}, 'category': 'kr_index'},
    {'key': 'kosdaq', 'label': 'KOSDAQ', 'providerSymbol': {'data_go_kr_index': '코스닥'}, 'category': 'kr_index'},
    {'key': 'kodex200', 'label': 'KODEX 200', 'providerSymbol': {'data_go_kr_etf': '069500'}, 'category': 'kr_etf_proxy'},
    {'key': 'tiger200', 'label': 'TIGER 200', 'providerSymbol': {'data_go_kr_etf': '102110'}, 'category': 'kr_etf_proxy'},
]


def secret_or_env(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value.strip()
    path = SECRETS_DIR / name
    if path.exists():
        return path.read_text(encoding='utf-8').strip()
    return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def fetch_json(url: str, timeout: int = 12) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={'User-Agent': 'PolarMeter-free-cache-poc/0.1'})
    with urllib.request.urlopen(req, timeout=timeout) as res:  # nosec - user-requested public API probe
        raw = res.read().decode('utf-8')
    return json.loads(raw)


def data_go_kr_url(endpoint: str, api_key: str, params: dict[str, str]) -> str:
    # DATA_GO_KR_SERVICE_KEY stores the portal's Encoding key. Keep it raw in the
    # query string to avoid double-encoding `%` into `%25`, which returns 401.
    return endpoint + '?serviceKey=' + api_key + '&' + urllib.parse.urlencode(params)


def data_go_kr_probe(api_key: str | None) -> dict[str, Any]:
    provider = 'data-go-kr-free'
    if not api_key:
        return {'provider': provider, 'status': 'missing_key', 'items': [], 'message': 'DATA_GO_KR_SERVICE_KEY not set'}
    endpoint = 'https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo'
    items = []
    for signal in REQUIRED_SIGNALS:
        symbol = signal.get('providerSymbol', {}).get('data_go_kr')
        if not symbol:
            continue
        params = {'resultType': 'json', 'pageNo': '1', 'numOfRows': '1', 'likeSrtnCd': symbol}
        url = data_go_kr_url(endpoint, api_key, params)
        try:
            data = fetch_json(url)
            body = data.get('response', {}).get('body', {})
            raw_items = body.get('items', {}).get('item')
            if isinstance(raw_items, list):
                row = raw_items[0] if raw_items else {}
            elif isinstance(raw_items, dict):
                row = raw_items
            else:
                row = {}
            status = 'ok' if row.get('clpr') is not None else 'unavailable'
            reason = None if status == 'ok' else 'no_price'
        except Exception as exc:  # pragma: no cover - network diagnostic
            row = {}
            status = 'error'
            reason = str(exc)
        items.append({
            'key': signal['key'],
            'label': signal['label'],
            'symbol': symbol,
            'status': status,
            'price': row.get('clpr'),
            'change': row.get('vs'),
            'changePct': row.get('fltRt'),
            'asOf': row.get('basDt'),
            'reason': reason,
        })
    return {'provider': provider, 'status': summarize(items), 'items': items}


def data_go_kr_index_probe(api_key: str | None) -> dict[str, Any]:
    provider = 'data-go-kr-index-free'
    if not api_key:
        return {'provider': provider, 'status': 'missing_key', 'items': [], 'message': 'DATA_GO_KR_SERVICE_KEY not set'}
    endpoint = 'https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getStockMarketIndex'
    items = []
    for signal in REQUIRED_SIGNALS:
        idx_name = signal.get('providerSymbol', {}).get('data_go_kr_index')
        if not idx_name:
            continue
        params = {'resultType': 'json', 'pageNo': '1', 'numOfRows': '1', 'idxNm': idx_name}
        url = data_go_kr_url(endpoint, api_key, params)
        try:
            data = fetch_json(url)
            body = data.get('response', {}).get('body', {})
            raw_items = body.get('items', {}).get('item')
            if isinstance(raw_items, list):
                row = raw_items[0] if raw_items else {}
            elif isinstance(raw_items, dict):
                row = raw_items
            else:
                row = {}
            status = 'ok' if row.get('clpr') is not None else 'unavailable'
            reason = None if status == 'ok' else 'no_price'
        except Exception as exc:  # pragma: no cover - network diagnostic
            row = {}
            status = 'blocked_unapplied'
            reason = str(exc)
        items.append({
            'key': signal['key'],
            'label': signal['label'],
            'symbol': idx_name,
            'status': status,
            'price': row.get('clpr'),
            'change': row.get('vs'),
            'changePct': row.get('fltRt'),
            'asOf': row.get('basDt'),
            'reason': reason,
        })
    return {'provider': provider, 'status': summarize(items), 'items': items, 'message': 'data.go.kr 15094807 금융위원회_지수시세정보'}


def data_go_kr_etf_probe(api_key: str | None) -> dict[str, Any]:
    provider = 'data-go-kr-etf-free'
    if not api_key:
        return {'provider': provider, 'status': 'missing_key', 'items': [], 'message': 'DATA_GO_KR_SERVICE_KEY not set'}
    endpoint = 'https://apis.data.go.kr/1160100/service/GetSecuritiesProductInfoService/getETFPriceInfo'
    items = []
    for signal in REQUIRED_SIGNALS:
        symbol = signal.get('providerSymbol', {}).get('data_go_kr_etf')
        if not symbol:
            continue
        params = {'resultType': 'json', 'pageNo': '1', 'numOfRows': '1', 'likeSrtnCd': symbol}
        url = data_go_kr_url(endpoint, api_key, params)
        try:
            data = fetch_json(url)
            body = data.get('response', {}).get('body', {})
            raw_items = body.get('items', {}).get('item')
            if isinstance(raw_items, list):
                row = raw_items[0] if raw_items else {}
            elif isinstance(raw_items, dict):
                row = raw_items
            else:
                row = {}
            status = 'ok' if row.get('clpr') is not None else 'unavailable'
            reason = None if status == 'ok' else 'no_price'
        except Exception as exc:  # pragma: no cover - network diagnostic
            row = {}
            status = 'blocked_unapplied'
            reason = str(exc)
        items.append({
            'key': signal['key'],
            'label': signal['label'],
            'symbol': symbol,
            'status': status,
            'price': row.get('clpr'),
            'change': row.get('vs'),
            'changePct': row.get('fltRt'),
            'asOf': row.get('basDt'),
            'nav': row.get('nav'),
            'basisIndexName': row.get('bssIdxIdxNm'),
            'basisIndexClose': row.get('bssIdxClpr'),
            'reason': reason,
        })
    return {'provider': provider, 'status': summarize(items), 'items': items, 'message': 'data.go.kr 15094806 금융위원회_증권상품시세정보'}


def twelve_probe(api_key: str | None) -> dict[str, Any]:
    provider = 'twelvedata-free'
    if not api_key:
        return {'provider': provider, 'status': 'missing_key', 'items': [], 'message': 'TWELVE_DATA_API_KEY not set'}
    items = []
    for signal in REQUIRED_SIGNALS:
        symbol = signal.get('providerSymbol', {}).get('twelvedata')
        if not symbol:
            continue
        qs = urllib.parse.urlencode({'symbol': symbol, 'apikey': api_key})
        url = f'https://api.twelvedata.com/price?{qs}'
        try:
            data = fetch_json(url)
            status = 'ok' if 'price' in data else 'unavailable'
            reason = None if status == 'ok' else data.get('message') or data.get('status') or 'no_price'
        except Exception as exc:  # pragma: no cover - network diagnostic
            data = {}
            status = 'error'
            reason = str(exc)
        items.append({
            'key': signal['key'],
            'label': signal['label'],
            'symbol': symbol,
            'status': status,
            'price': data.get('price'),
            'reason': reason,
        })
    return {'provider': provider, 'status': summarize(items), 'items': items}


def fmp_probe(api_key: str | None) -> dict[str, Any]:
    provider = 'fmp-free'
    if not api_key:
        return {'provider': provider, 'status': 'missing_key', 'items': [], 'message': 'FMP_API_KEY not set'}
    items = []
    for signal in REQUIRED_SIGNALS:
        symbol = signal.get('providerSymbol', {}).get('fmp')
        if not symbol:
            continue
        qs = urllib.parse.urlencode({'apikey': api_key})
        url = f'https://financialmodelingprep.com/stable/quote-short?symbol={urllib.parse.quote(symbol)}&{qs}'
        try:
            data = fetch_json(url)
            row = data[0] if isinstance(data, list) and data else {}
            status = 'ok' if row.get('price') is not None else 'unavailable'
            reason = None if status == 'ok' else 'no_price'
        except Exception as exc:  # pragma: no cover - network diagnostic
            row = {}
            status = 'error'
            reason = str(exc)
        items.append({
            'key': signal['key'],
            'label': signal['label'],
            'symbol': symbol,
            'status': status,
            'price': row.get('price'),
            'reason': reason,
        })
    return {'provider': provider, 'status': summarize(items), 'items': items}


def summarize(items: list[dict[str, Any]]) -> str:
    if not items:
        return 'unavailable'
    ok = sum(1 for item in items if item.get('status') == 'ok')
    if ok == len(items):
        return 'ok'
    if ok:
        return 'partial'
    return 'unavailable'


def build_report() -> dict[str, Any]:
    providers = [
        twelve_probe(secret_or_env('TWELVE_DATA_API_KEY')),
        fmp_probe(secret_or_env('FMP_API_KEY')),
        data_go_kr_probe(secret_or_env('DATA_GO_KR_SERVICE_KEY')),
        data_go_kr_index_probe(secret_or_env('DATA_GO_KR_SERVICE_KEY')),
        data_go_kr_etf_probe(secret_or_env('DATA_GO_KR_SERVICE_KEY')),
    ]
    return {
        'mode': 'free_provider_probe',
        'generatedAt': utc_now(),
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'requiredSignals': REQUIRED_SIGNALS,
        'providers': providers,
        'status': 'needs_keys' if all(p['status'] == 'missing_key' for p in providers) else summarize_provider_status(providers),
    }


def summarize_provider_status(providers: list[dict[str, Any]]) -> str:
    statuses = [p.get('status') for p in providers]
    if any(status == 'ok' for status in statuses):
        return 'ok'
    if any(status == 'partial' for status in statuses):
        return 'partial'
    return 'unavailable'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"PolarMeter free provider probe: {report['status']}")
        for provider in report['providers']:
            print(f"- {provider['provider']}: {provider['status']}")
            if provider.get('message'):
                print(f"  {provider['message']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
