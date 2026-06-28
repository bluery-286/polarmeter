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
    {'key': 'vix', 'label': 'CBOE VIX', 'providerSymbol': {'yahoo_chart': '^VIX', 'twelvedata': 'VIX', 'fmp': '^VIX'}, 'category': 'volatility'},
    {'key': 'usd_krw', 'label': 'USD/KRW', 'providerSymbol': {'bok_ecos': '0000001', 'twelvedata': 'USD/KRW', 'fmp': 'USD/KRW'}, 'category': 'fx'},
    {'key': 'us10y', 'label': 'US 10Y Treasury Yield', 'providerSymbol': {'yahoo_chart': '^TNX', 'twelvedata': 'TLT', 'fmp': 'TLT'}, 'category': 'rate'},
    {'key': 'dxy', 'label': 'US Dollar Index', 'providerSymbol': {'yahoo_chart': 'DX-Y.NYB', 'twelvedata': 'UUP', 'fmp': 'UUP'}, 'category': 'dollar'},
    {'key': 'wti', 'label': 'WTI proxy/USO', 'providerSymbol': {'twelvedata': 'USO', 'fmp': 'USO'}, 'category': 'commodity'},
    {'key': 'gold', 'label': 'Gold proxy/GLD', 'providerSymbol': {'twelvedata': 'GLD', 'fmp': 'GLD'}, 'category': 'commodity'},
    {'key': 'soxx', 'label': 'SOXX', 'providerSymbol': {'twelvedata': 'SOXX', 'fmp': 'SOXX'}, 'category': 'semiconductor'},
    {'key': 'smh', 'label': 'SMH', 'providerSymbol': {'twelvedata': 'SMH', 'fmp': 'SMH'}, 'category': 'semiconductor'},
    {'key': 'iwm', 'label': 'Russell 2000/IWM', 'providerSymbol': {'yahoo_chart': 'IWM', 'twelvedata': 'IWM', 'fmp': 'IWM'}, 'category': 'us_smallcap'},
    {'key': 'eem', 'label': 'Emerging Markets/EEM', 'providerSymbol': {'yahoo_chart': 'EEM', 'twelvedata': 'EEM', 'fmp': 'EEM'}, 'category': 'global_equity'},
    {'key': 'kr_samsung', 'label': '삼성전자', 'providerSymbol': {'data_go_kr': '005930'}, 'category': 'kr_proxy'},
    {'key': 'kospi', 'label': 'KOSPI', 'providerSymbol': {'yahoo_chart': '^KS11', 'data_go_kr_index': '코스피'}, 'category': 'kr_index'},
    {'key': 'kosdaq', 'label': 'KOSDAQ', 'providerSymbol': {'yahoo_chart': '^KQ11', 'data_go_kr_index': '코스닥'}, 'category': 'kr_index'},
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


def first_present(data: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ''):
            return value
    return None


def bok_key() -> str | None:
    return secret_or_env('BOK_API_KEY') or secret_or_env('ECOS_API_KEY') or secret_or_env('BANK_OF_KOREA_API_KEY')


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


def yahoo_chart_probe() -> dict[str, Any]:
    """Worker-side public delayed Yahoo chart probe for non-keyed macro/index gaps.

    The mobile app never receives direct provider URLs; it only receives the
    normalized cache snapshot produced by this worker.
    """
    provider = 'public-chart-delayed'
    items: list[dict[str, Any]] = []
    for signal in REQUIRED_SIGNALS:
        symbol = signal.get('providerSymbol', {}).get('yahoo_chart')
        if not symbol:
            continue
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol, safe="")}?range=5d&interval=1d'
        try:
            data = fetch_json(url)
            result = (data.get('chart', {}).get('result') or [{}])[0]
            meta = result.get('meta') or {}
            quote = (result.get('indicators', {}).get('quote') or [{}])[0]
            closes = [value for value in (quote.get('close') or []) if value is not None]
            price = meta.get('regularMarketPrice') or (closes[-1] if closes else None)
            previous = closes[-2] if len(closes) >= 2 else None
            change = None
            change_pct = None
            if price is not None and previous not in (None, 0):
                change = float(price) - float(previous)
                change_pct = change / float(previous) * 100
            status = 'ok' if price is not None else 'unavailable'
            reason = None if status == 'ok' else 'no_price'
        except Exception as exc:  # pragma: no cover - network diagnostic
            price = change = change_pct = None
            meta = {}
            status = 'error'
            reason = str(exc)
        items.append({
            'key': signal['key'],
            'label': signal['label'],
            'symbol': symbol,
            'status': status,
            'price': price,
            'change': change,
            'changePct': change_pct,
            'asOf': meta.get('regularMarketTime'),
            'reason': reason,
        })
    return {'provider': provider, 'status': summarize(items), 'items': items, 'message': 'Public delayed chart worker-side fetch; app receives normalized snapshot only'}


def bok_ecos_fx_probe(api_key: str | None) -> dict[str, Any]:
    """Probe Bank of Korea ECOS daily USD/KRW reference rate.

    The cache worker may use a BOK key, but the app never receives that key or a
    direct ECOS URL. If no BOK key exists, this remains a clean missing-key item.
    """
    provider = 'bok-ecos-free'
    if not api_key:
        return {'provider': provider, 'status': 'missing_key', 'items': [], 'message': 'BOK_API_KEY/ECOS_API_KEY not set'}
    from datetime import timedelta

    items = []
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=14)
    for signal in REQUIRED_SIGNALS:
        item_code = signal.get('providerSymbol', {}).get('bok_ecos')
        if not item_code:
            continue
        # 731Y001: daily major exchange rates; 0000001 is USD/KRW in ECOS.
        url = (
            f'https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(api_key)}/json/kr/1/20/'
            f'731Y001/D/{start:%Y%m%d}/{end:%Y%m%d}/{item_code}'
        )
        try:
            data = fetch_json(url)
            rows = data.get('StatisticSearch', {}).get('row') or []
            rows = rows if isinstance(rows, list) else [rows]
            row = rows[-1] if rows else {}
            prev = rows[-2] if len(rows) >= 2 else {}
            price = row.get('DATA_VALUE')
            prev_price = prev.get('DATA_VALUE')
            change = None
            change_pct = None
            try:
                if price not in (None, '') and prev_price not in (None, ''):
                    change = float(price) - float(prev_price)
                    change_pct = change / float(prev_price) * 100 if float(prev_price) else None
            except (TypeError, ValueError, ZeroDivisionError):
                change = None
                change_pct = None
            status = 'ok' if price is not None else 'unavailable'
            reason = None if status == 'ok' else data.get('RESULT', {}).get('MESSAGE') or 'no_price'
        except Exception as exc:  # pragma: no cover - network diagnostic
            row = {}
            price = change = change_pct = None
            status = 'error'
            reason = str(exc)
        items.append({
            'key': signal['key'],
            'label': signal['label'],
            'symbol': 'BOK_ECOS:731Y001:0000001',
            'status': status,
            'price': price,
            'change': change,
            'changePct': change_pct,
            'asOf': row.get('TIME'),
            'reason': reason,
        })
    return {'provider': provider, 'status': summarize(items), 'items': items, 'message': 'Bank of Korea ECOS daily USD/KRW reference rate'}


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
        url = f'https://api.twelvedata.com/quote?{qs}'
        try:
            data = fetch_json(url)
            price = first_present(data, ['close', 'price'])
            status = 'ok' if price is not None else 'unavailable'
            reason = None if status == 'ok' else data.get('message') or data.get('status') or 'no_price'
        except Exception as exc:  # pragma: no cover - network diagnostic
            data = {}
            price = None
            status = 'error'
            reason = str(exc)
        items.append({
            'key': signal['key'],
            'label': signal['label'],
            'symbol': symbol,
            'status': status,
            'price': price,
            'change': data.get('change'),
            'changePct': data.get('percent_change'),
            'asOf': data.get('timestamp') or data.get('datetime'),
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
        url = f'https://financialmodelingprep.com/stable/quote?symbol={urllib.parse.quote(symbol)}&{qs}'
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
            'change': row.get('change'),
            'changePct': row.get('changePercentage'),
            'asOf': row.get('timestamp'),
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
        bok_ecos_fx_probe(bok_key()),
        yahoo_chart_probe(),
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
