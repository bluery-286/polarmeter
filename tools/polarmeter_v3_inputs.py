#!/usr/bin/env python3
"""Worker-side inputs for temperature v3: daily bars, KR investor flows, link signals.

Sources are public and delayed: Yahoo chart daily bars (same endpoint as the
v2 probe) and Naver Finance's public index pages for KR investor net buying and
the official previous close. The app never calls these; it receives only the
normalised public snapshot. Failures here never block the v2 publish.
"""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import polarmeter_score_core_v3 as v3

KST = ZoneInfo('Asia/Seoul')
YAHOO_DAILY_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d'
NAVER_FLOW_URL = 'https://m.stock.naver.com/api/index/{market}/trend?bizdate={day}'
NAVER_PRICE_URL = 'https://m.stock.naver.com/api/index/{market}/price?pageSize=2&page=1'
REQUEST_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PolarMeterCache/1.0)'}
FLOW_RETENTION_SESSIONS = 80
FLOW_BACKFILL_PER_RUN = 25
FLOW_SETTLE_TIME_KST = (15, 40)
PREVIOUS_CLOSE_TOLERANCE = 0.003
FLOW_SOURCE_ID = 'public-investor-flow-delayed'

Fetch = Callable[[str], Any]


def fetch_json(url: str, timeout: int = 15) -> Any:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = str(value or '').replace(',', '').replace('+', '').strip()
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def series_from_chart(payload: dict[str, Any]) -> v3.Series | None:
    result = (payload.get('chart', {}).get('result') or [None])[0]
    if not isinstance(result, dict):
        return None
    meta = result.get('meta') or {}
    zone = ZoneInfo(str(meta.get('exchangeTimezoneName') or 'UTC'))
    stamps = result.get('timestamp') or []
    closes = ((result.get('indicators', {}).get('quote') or [{}])[0]).get('close') or []
    points = {}
    for ts, close in zip(stamps, closes):
        points[datetime.fromtimestamp(float(ts), zone).date().isoformat()] = number(close)
    # Today's bar can lag the quote; the delayed quote is today's value.
    price, quote_time = number(meta.get('regularMarketPrice')), meta.get('regularMarketTime')
    if price is not None and isinstance(quote_time, (int, float)):
        quote_day = datetime.fromtimestamp(float(quote_time), zone).date().isoformat()
        if quote_day in points and points[quote_day] is None:
            points[quote_day] = price
    return v3.Series(points.items()) if points else None


def fetch_daily_series(symbols: tuple[str, ...] = v3.SYMBOLS, fetch: Fetch = fetch_json,
                       pause: float = 0.15) -> tuple[dict[str, v3.Series], list[str]]:
    series: dict[str, v3.Series] = {}
    failed: list[str] = []
    for symbol in symbols:
        try:
            parsed = series_from_chart(fetch(YAHOO_DAILY_URL.format(symbol=urllib.parse.quote(symbol, safe=''))))
        except Exception:  # pragma: no cover - network diagnostic
            parsed = None
        if parsed:
            series[symbol] = parsed
        else:
            failed.append(symbol)
        if pause:
            time.sleep(pause)
    return series, failed


def flow_row_settled(day: str, fetched_at: datetime) -> bool:
    close = datetime.fromisoformat(day).replace(hour=FLOW_SETTLE_TIME_KST[0], minute=FLOW_SETTLE_TIME_KST[1], tzinfo=KST)
    return fetched_at >= close


def fetch_flow_day(day: str, fetch: Fetch) -> dict[str, dict[str, float]] | None:
    row = {}
    for market in ('KOSPI', 'KOSDAQ'):
        payload = fetch(NAVER_FLOW_URL.format(market=market, day=day.replace('-', '')))
        if not isinstance(payload, dict) or payload.get('bizdate') != day.replace('-', ''):
            return None
        values = {actor: number(payload.get(field)) for actor, field in (
            ('foreign', 'foreignValue'), ('institution', 'institutionalValue'), ('personal', 'personalValue'))}
        if any(v is None for v in values.values()):
            return None
        row[market.lower()] = values
    return row


def update_investor_flows(previous: dict[str, Any] | None, kr_sessions: list[str], now: datetime,
                          fetch: Fetch = fetch_json, pause: float = 0.2) -> dict[str, Any]:
    """Keep a per-session ledger; refetch only today and rows stored before their close."""
    items = {item['date']: item for item in ((previous or {}).get('items') or [])
             if isinstance(item, dict) and isinstance(item.get('date'), str)}
    wanted = kr_sessions[-FLOW_RETENTION_SESSIONS:]
    today = wanted[-1] if wanted else None
    stale = [d for d in wanted if d not in items or not items[d].get('settled')]
    # Newest first so the score has today and recent history before old backfill.
    targets = ([today] if today else []) + [d for d in reversed(stale) if d != today][:FLOW_BACKFILL_PER_RUN]
    fetched_at = now.astimezone(KST)
    errors = 0
    for day in targets:
        try:
            row = fetch_flow_day(day, fetch)
        except Exception:  # pragma: no cover - network diagnostic
            row, errors = None, errors + 1
        if pause:
            time.sleep(pause)
        if row:
            items[day] = {'date': day, **row, 'settled': flow_row_settled(day, fetched_at),
                          'fetchedAt': now.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}
    kept = [items[d] for d in sorted(items) if d in set(wanted)]
    return {
        'version': 'investor-flows-v1',
        'sourceId': FLOW_SOURCE_ID,
        'sourceLabel': '네이버 증권 (지연)',
        'unit': 'KRW_100M',
        'updatedAt': now.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'status': 'ok' if today in items else 'unavailable',
        'fetchErrors': errors,
        'items': kept,
    }


def flows_by_date(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item['date']: item for item in ledger.get('items') or []}


def naver_previous_close(market: str, session_day: str, fetch: Fetch = fetch_json) -> float | None:
    """Official close of the session before `session_day`, from Naver's daily rows."""
    rows = fetch(NAVER_PRICE_URL.format(market=market))
    if not isinstance(rows, list) or len(rows) < 2 or rows[0].get('localTradedAt') != session_day:
        return None
    return number(rows[1].get('closePrice'))


def cross_check_kr_previous_close(signals: dict[str, Any], now: datetime, fetch: Fetch = fetch_json) -> list[dict[str, Any]]:
    """Recompute KOSPI/KOSDAQ change from the official previous close when the quote's base is off.

    2026-11-04: a missing Yahoo bar made the base the 11/02 close and published a
    flat open as -3.54%. The check only applies to a quote dated today in KST.
    """
    applied = []
    for key, market in (('kospi', 'KOSPI'), ('kosdaq', 'KOSDAQ')):
        signal = signals.get(key)
        if not isinstance(signal, dict):
            continue
        value, base = number(signal.get('value')), number(signal.get('previousClose'))
        observed = signal.get('dataAsOf')
        try:
            day = datetime.fromisoformat(str(observed).replace('Z', '+00:00')).astimezone(KST).date().isoformat()
            official = naver_previous_close(market, day, fetch)
        except Exception:
            continue
        if value is None or official is None:
            continue
        if base is not None and abs(base / official - 1) <= PREVIOUS_CLOSE_TOLERANCE:
            continue
        signal['previousClose'] = official
        signal['change'] = value - official
        signal['changePct'] = (value / official - 1) * 100
        signal['previousCloseCheck'] = {'source': 'naver-finance-public', 'corrected': True,
                                        'replacedPreviousClose': base}
        applied.append({'key': key, 'from': base, 'to': official})
    return applied


def _pct_text(value: float | None) -> str:
    return '확인 중' if value is None else f'{value:+.1f}%'


def link_signals(series: dict[str, v3.Series], flows: dict[str, dict[str, Any]], kr_day: str | None) -> list[dict[str, Any]]:
    """Four numbers that carry the US session into the KR session, each with a plain reading."""
    if not kr_day:
        return []
    rows: list[dict[str, Any]] = []

    def last(symbol: str, before: bool = False) -> tuple[str | None, float | None, float | None]:
        s = series.get(symbol)
        if not s:
            return None, None, None
        i = s.index_before(kr_day) if before else s.index_on_or_before(kr_day)
        if i < 1:
            return None, None, None
        return s.dates[i], s.closes[i], s.ret(i)

    fx_day, fx_value, fx_pct = last('KRW=X')
    if fx_value is not None:
        tone = 'minus' if (fx_pct or 0) >= 0.4 else 'plus' if (fx_pct or 0) <= -0.4 else 'neutral'
        read = {'minus': ('원화 약세: 외국인 자금에 부담', 'Weaker won: a headwind for foreign money'),
                'plus': ('원화 강세: 외국인 자금에 우호', 'Stronger won: supportive for foreign money'),
                'neutral': ('환율 변화 작음', 'Little change in the won')}[tone]
        rows.append({'key': 'usd_krw', 'nameKo': '원/달러 환율', 'nameEn': 'USD/KRW', 'value': f'{fx_value:,.1f}원',
                     'changePct': fx_pct, 'tone': tone, 'readKo': f'{_pct_text(fx_pct)} · {read[0]}',
                     'readEn': f'{_pct_text(fx_pct)} · {read[1]}', 'asOf': fx_day})
    ewy_day, ewy_value, ewy_pct = last('EWY', before=True)
    if ewy_pct is not None:
        tone = 'plus' if ewy_pct >= 0.5 else 'minus' if ewy_pct <= -0.5 else 'neutral'
        read = {'plus': ('미국 투자자가 한국 주식을 더 샀어요', 'US investors bid up Korean stocks'),
                'minus': ('미국 투자자가 한국 주식을 팔았어요', 'US investors sold Korean stocks'),
                'neutral': ('밤사이 큰 변화 없음', 'Little overnight change')}[tone]
        rows.append({'key': 'ewy', 'nameKo': '밤사이 미국 상장 한국 ETF', 'nameEn': 'Korea ETF in New York overnight',
                     'value': _pct_text(ewy_pct), 'changePct': ewy_pct, 'tone': tone,
                     'readKo': f'{ewy_day} 미국장 · {read[0]}', 'readEn': f'{ewy_day} US session · {read[1]}', 'asOf': ewy_day})
    today = flows.get(kr_day)
    if today:
        net = sum(float((today.get(m) or {}).get('foreign') or 0) for m in ('kospi', 'kosdaq'))
        history = [sum(float((flows[d].get(m) or {}).get('foreign') or 0) for m in ('kospi', 'kosdaq'))
                   for d in sorted(flows) if d < kr_day][-v3.FLOW_HISTORY_DAYS:]
        typical = math.sqrt(sum(v * v for v in history) / len(history)) if history else None
        tone = 'plus' if net > 0 else 'minus' if net < 0 else 'neutral'
        size = '' if not typical else f' · 평소 규모의 {abs(net) / typical:.1f}배'
        size_en = '' if not typical else f' · {abs(net) / typical:.1f}x a typical day'
        action = ('순매수', 'net buying') if net > 0 else ('순매도', 'net selling') if net < 0 else ('보합', 'flat')
        rows.append({'key': 'foreign_flow', 'nameKo': '외국인 순매수 (코스피+코스닥)', 'nameEn': 'Foreign net buying (KOSPI+KOSDAQ)',
                     'value': f'{net:+,.0f}억', 'changePct': None, 'tone': tone,
                     'readKo': f'외국인 {action[0]}{size}' + ('' if today.get('settled') else ' · 장중 누적'),
                     'readEn': f'Foreign {action[1]}{size_en}' + ('' if today.get('settled') else ' · intraday'),
                     'asOf': kr_day})
    us_day, _, soxx_pct = last('SOXX', before=True)
    kr_semi_day, _, kr_semi_pct = last('091160.KS')
    if soxx_pct is not None and kr_semi_pct is not None and kr_semi_day == kr_day:
        same = (soxx_pct >= 0) == (kr_semi_pct >= 0)
        tone = ('plus' if kr_semi_pct >= 0 else 'minus') if same else 'neutral'
        read = ('미국·한국 반도체가 같은 방향' if same else '미국 반도체 흐름이 한국으로 이어지지 않음',
                'US and Korean chips moved together' if same else 'US chip move did not carry into Korea')
        rows.append({'key': 'semis_link', 'nameKo': '반도체 미국 → 한국', 'nameEn': 'Chips: US → Korea',
                     'value': f'미국 {_pct_text(soxx_pct)} · 한국 {_pct_text(kr_semi_pct)}', 'changePct': kr_semi_pct,
                     'tone': tone, 'readKo': read[0], 'readEn': read[1], 'asOf': kr_day})
    return rows


def apply_v3_layers(snapshot: dict[str, Any], previous_public: dict[str, Any] | None, now: datetime | None = None,
                    fetch: Fetch = fetch_json, pause: float = 0.15) -> dict[str, Any]:
    """Attach scoreCoreV3, investorFlows and linkSignals. Never raises."""
    now = now or datetime.now(timezone.utc)
    try:
        series, failed = fetch_daily_series(fetch=fetch, pause=pause)
        kr_sessions = v3.session_dates(series, '^KS11')
        ledger = update_investor_flows((previous_public or {}).get('investorFlows'), kr_sessions, now, fetch, pause)
        flows = flows_by_date(ledger)
        signals = snapshot.get('signals') or {}
        index_pct = {symbol: number((signals.get(key) or {}).get('changePct'))
                     for symbol, key in (('^KS11', 'kospi'), ('^KQ11', 'kosdaq'), ('SPY', 'sp500'),
                                         ('QQQ', 'nasdaq100'), ('IWM', 'iwm'))
                     if (signals.get(key) or {}).get('status') in {'ok', 'suspect'}}
        core = v3.build_score_core_v3(series, flows, snapshot.get('generatedAt'), index_pct)
        core['missingInputs'] = failed
        snapshot['scoreCoreV3'] = core
        snapshot['investorFlows'] = ledger
        snapshot['linkSignals'] = link_signals(series, flows, kr_sessions[-1] if kr_sessions else None)
    except Exception as error:  # pragma: no cover - keep v2 publishing
        snapshot['scoreCoreV3'] = {'schemaVersion': v3.SCHEMA_VERSION, 'formulaVersion': v3.FORMULA_VERSION,
                                   'generatedAt': snapshot.get('generatedAt'), 'status': 'unavailable',
                                   'reason': type(error).__name__}
        snapshot.setdefault('investorFlows', (previous_public or {}).get('investorFlows'))
        snapshot.setdefault('linkSignals', [])
    return snapshot
