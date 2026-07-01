#!/usr/bin/env python3
"""PolarMeter B1 candidate scoring core.

Single source of truth for PolarMeter Lite temperatures used by the app tooling
and the morning briefing. This is market-environment scoring, not investment
advice or a buy/sell signal.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Any

KST = timezone(timedelta(hours=9))

SESSION_TYPES = {'normal', 'holiday_kr', 'holiday_us', 'weekend', 'pre_market', 'after_market'}

WEIGHT_PROFILES = {
    'normal': {
        'us': {'index_momentum': 0.25, 'volatility': 0.20, 'macro': 0.20, 'sector': 0.20, 'sentiment_placeholder': 0.15},
        'kr': {'index_momentum': 0.25, 'supply_placeholder': 0.25, 'currency': 0.20, 'industry': 0.15, 'domestic_news_placeholder': 0.15},
    },
    'holiday_kr': {
        'us': {'index_momentum': 0.24, 'volatility': 0.21, 'macro': 0.21, 'sector': 0.20, 'sentiment_placeholder': 0.14},
        'kr': {'index_momentum': 0.12, 'supply_placeholder': 0.08, 'currency': 0.26, 'industry': 0.22, 'domestic_news_placeholder': 0.32},
    },
    'holiday_us': {
        'us': {'index_momentum': 0.13, 'volatility': 0.18, 'macro': 0.25, 'sector': 0.17, 'sentiment_placeholder': 0.27},
        'kr': {'index_momentum': 0.25, 'supply_placeholder': 0.24, 'currency': 0.22, 'industry': 0.15, 'domestic_news_placeholder': 0.14},
    },
    'weekend': {
        'us': {'index_momentum': 0.10, 'volatility': 0.18, 'macro': 0.24, 'sector': 0.13, 'sentiment_placeholder': 0.35},
        'kr': {'index_momentum': 0.10, 'supply_placeholder': 0.05, 'currency': 0.26, 'industry': 0.14, 'domestic_news_placeholder': 0.45},
    },
    'pre_market': {
        'us': {'index_momentum': 0.18, 'volatility': 0.20, 'macro': 0.24, 'sector': 0.18, 'sentiment_placeholder': 0.20},
        'kr': {'index_momentum': 0.18, 'supply_placeholder': 0.12, 'currency': 0.26, 'industry': 0.18, 'domestic_news_placeholder': 0.26},
    },
    'after_market': {
        'us': {'index_momentum': 0.16, 'volatility': 0.19, 'macro': 0.23, 'sector': 0.17, 'sentiment_placeholder': 0.25},
        'kr': {'index_momentum': 0.16, 'supply_placeholder': 0.10, 'currency': 0.25, 'industry': 0.19, 'domestic_news_placeholder': 0.30},
    },
}

# P0 guard: semiconductor is an important signal, but one sector must not make
# the total market thermometer look like a sector ETF thermometer. Keep raw
# component scores intact; cap only the contribution delta to the total score.
US_SECTOR_DELTA_CAP = 8.0
KR_INDUSTRY_DELTA_CAP = 6.0

# Short pressure is a separate P0-light layer. It must not be mixed into the
# current US/KR temperatures: temperature explains the current market state;
# short_pressure explains 1~3 trading-day risk/relief/volatility pressure.
SHORT_PRESSURE_WEIGHTS = {'lagging': 0.15, 'coincident': 0.35, 'leading': 0.50}
SHORT_PRESSURE_DISCLAIMER = '단기 압력은 시장 환경 정보이며 투자 권유나 방향 예측이 아닙니다.'

SOURCE_IDS = {
    'S&P500': 'yahoo:^GSPC',
    'S&P500/SPY': 'yahoo:^GSPC',
    'SPY': 'yahoo:^GSPC',
    'Nasdaq100': 'yahoo:^NDX',
    'Nasdaq100/QQQ': 'yahoo:^NDX',
    'QQQ': 'yahoo:^NDX',
    'Russell 2000': 'yahoo:IWM',
    'Russell 2000/IWM': 'yahoo:IWM',
    'IWM': 'yahoo:IWM',
    'VIX': 'yahoo:^VIX',
    '미국 10년물': 'yahoo:^TNX',
    'DXY': 'yahoo:DX-Y.NYB',
    'USD/KRW': 'yahoo:KRW=X',
    'KOSPI': 'naver:kospi',
    'KOSDAQ': 'naver:kosdaq',
    'SOXX': 'yahoo:SOXX',
    'SMH': 'yahoo:SMH',
    '삼성전자': 'naver:005930',
    'SK하이닉스': 'naver:000660',
}


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def pct(items: dict[str, dict], key: str) -> float | None:
    item = items.get(key) or {}
    value = item.get('pct')
    return float(value) if value is not None else None


def pct_any(items: dict[str, dict], *keys: str) -> float | None:
    for key in keys:
        value = pct(items, key)
        if value is not None:
            return value
    return None


def close(items: dict[str, dict], key: str) -> float | None:
    item = items.get(key) or {}
    value = item.get('close')
    return float(value) if value is not None else None


def avg(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None and not math.isnan(v)]
    if not xs:
        return None
    return sum(xs) / len(xs)


def weighted_average(components: dict[str, float], weights: dict[str, float], contribution_delta_caps: dict[str, float] | None = None) -> float:
    total_weight = sum(weights.values()) or 1.0
    contribution_delta_caps = contribution_delta_caps or {}
    total = 0.0
    for key, weight in weights.items():
        raw_contribution = components[key] * weight
        cap = contribution_delta_caps.get(key)
        if cap is not None:
            neutral_contribution = 50.0 * weight
            delta = raw_contribution - neutral_contribution
            raw_contribution = neutral_contribution + clamp(delta, -cap, cap)
        total += raw_contribution
    return total / total_weight


US_BROAD_ALL_DOWN_CAP = 54.0
US_NASDAQ_STRESS_CAP = 49.0
US_NASDAQ_STRESS_THRESHOLD_PCT = -1.5


def us_broad_market_guardrail(raw_score: float, sp500_pct: float | None, nasdaq_pct: float | None, smallcap_pct: float | None) -> tuple[float, dict[str, Any]]:
    """Cap US temperature when broad index stress contradicts a warm score.

    Percent inputs use the existing provider convention: -1.5 means -1.5%,
    not -0.015. Raw component scores stay intact; only the final displayed
    US market temperature is capped to avoid a misleading warm label.
    """
    all_down = all(v is not None and v < 0 for v in (sp500_pct, nasdaq_pct, smallcap_pct))
    nasdaq_stressed = nasdaq_pct is not None and nasdaq_pct <= US_NASDAQ_STRESS_THRESHOLD_PCT and (
        (sp500_pct is not None and sp500_pct < 0) or (smallcap_pct is not None and smallcap_pct < 0)
    )
    score = raw_score
    applied: list[str] = []
    if all_down:
        score = min(score, US_BROAD_ALL_DOWN_CAP)
        applied.append('all_down_cap')
    if nasdaq_stressed:
        score = min(score, US_NASDAQ_STRESS_CAP)
        applied.append('nasdaq_stress_cap')
    return score, {
        'applied': applied,
        'all_down': all_down,
        'nasdaq_stressed': nasdaq_stressed,
        'inputs': {
            'sp500_pct': sp500_pct,
            'nasdaq_pct': nasdaq_pct,
            'smallcap_pct': smallcap_pct,
        },
        'caps': {
            'all_down': US_BROAD_ALL_DOWN_CAP,
            'nasdaq_stress': US_NASDAQ_STRESS_CAP,
            'nasdaq_stress_threshold_pct': US_NASDAQ_STRESS_THRESHOLD_PCT,
        },
    }


def detect_session(snapshot: dict, override: str | None = None, now: datetime | None = None) -> str:
    if override:
        if override not in SESSION_TYPES:
            raise ValueError(f'unknown session type: {override}')
        return override
    explicit = snapshot.get('market_session_type') or snapshot.get('session_type')
    if explicit:
        return explicit if explicit in SESSION_TYPES else 'normal'
    now = now or datetime.now(KST)
    if now.weekday() >= 5:
        return 'weekend'
    hour_min = now.hour * 60 + now.minute
    if hour_min < 9 * 60:
        return 'pre_market'
    if hour_min >= 18 * 60:
        return 'after_market'
    return 'normal'


def freshness_for_status(status: str | None, session_type: str, price_like: bool = True) -> str:
    if status in {'invalid', 'unavailable'}:
        return 'unavailable'
    if status in {'delayed', 'stale'}:
        return 'stale_2d'
    if session_type in {'holiday_kr', 'holiday_us', 'weekend'} and price_like:
        return 'stale_1d'
    return 'live'


def confidence_from_freshness(signal_freshness: dict[str, str], session_type: str) -> float:
    values = list(signal_freshness.values()) or ['live']
    score_map = {'live': 1.0, 'stale_1d': 0.72, 'stale_2d': 0.45, 'stale_old': 0.2, 'unavailable': 0.0}
    base = sum(score_map.get(v, 0.5) for v in values) / len(values)
    if session_type == 'weekend':
        base *= 0.78
    elif session_type.startswith('holiday'):
        base *= 0.86
    elif session_type in {'pre_market', 'after_market'}:
        base *= 0.9
    return round(clamp(base, 0.1, 1.0), 2)


def component_from_pct(change_pct: float | None, multiplier: float, neutral: float = 50.0) -> float:
    if change_pct is None:
        return neutral
    return clamp(neutral + change_pct * multiplier)


def label(score: float | int | None) -> str:
    if score is None:
        return '데이터 부족'
    if score <= 20:
        return '극냉 · 공포'
    if score <= 40:
        return '냉각 · 리스크 높음'
    if score <= 60:
        return '중립 · 관망'
    if score <= 80:
        return '온기 · 긍정 우위'
    return '과열 · 변동성 경계'


def compact_label(score: int | None) -> str:
    return '데이터 부족' if score is None else label(score).replace(' · ', '·')


def pressure_level(score: float | int | None) -> str:
    if score is None:
        return 'normal'
    if score < 35:
        return 'low'
    if score < 55:
        return 'normal'
    if score < 70:
        return 'elevated'
    if score < 85:
        return 'high'
    return 'extreme'


def pressure_label(pressure_type: str, level: str) -> str:
    level_ko = {
        'low': '낮음',
        'normal': '보통',
        'elevated': '높아짐',
        'high': '높음',
        'extreme': '매우 높음',
    }.get(level, '보통')
    if pressure_type == 'data_limited':
        return '단기 압력 확인 중'
    if pressure_type == 'risk':
        return f'단기 부담 압력 {level_ko}'
    if pressure_type == 'relief':
        return f'완화 압력 {level_ko}'
    if pressure_type == 'volatility':
        return f'변동성 압력 {level_ko}'
    if pressure_type == 'mixed':
        return '단기 압력 혼재'
    return '단기 압력 보통'


def impact_from_pct(change_pct: float | None, up_text: str, down_text: str) -> str:
    if change_pct is None:
        return '영향 확인 중'
    return up_text if change_pct > 0 else down_text


def session_badge(session_type: str) -> str:
    return {
        'normal': '일반 기준',
        'holiday_kr': '한국장 휴장 기준',
        'holiday_us': '미국장 휴장 기준',
        'weekend': '주말 기준',
        'pre_market': '개장 전 기준',
        'after_market': '마감 후 기준',
    }.get(session_type, '일반 기준')


def session_window_summary(session_type: str) -> str:
    return {
        'weekend': '주말에는 가격이 멈춰 있어 다음 개장 전 뉴스·금리·환율을 더 확인합니다.',
        'pre_market': '개장 전에는 전 거래일 마감값과 밤사이 확인 신호를 함께 봅니다.',
        'after_market': '마감 후에는 해당 장의 마감값과 장후 확인 신호를 함께 봅니다.',
        'holiday_kr': '한국장이 쉬는 날에는 마지막 거래값과 열린 해외 신호를 참고합니다.',
        'holiday_us': '미국장이 쉬는 날에는 마지막 거래값과 열린 국내·글로벌 신호를 참고합니다.',
    }.get(session_type, '기준시각에 맞춰 신호 반영 강도를 조정합니다.')


def source_id_for(*labels: str) -> list[str]:
    ids: list[str] = []
    for item in labels:
        source_id = SOURCE_IDS.get(item)
        if source_id and source_id not in ids:
            ids.append(source_id)
    return ids


def confidence_level(score: float) -> str:
    if score < 0.45:
        return 'low'
    if score < 0.75:
        return 'medium'
    return 'high'


def build_short_pressure(rows: dict[str, dict], session_type: str, signal_freshness: dict[str, str], as_of: str | None = None) -> dict[str, Any]:
    """Build a transparent 1~3 trading-day pressure layer.

    This is deliberately rule-based for P0-light. It describes market-environment
    pressure only; it is not a direction forecast, accuracy score, or trading
    action signal.
    """
    drivers: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    buckets = {'risk': 0.0, 'relief': 0.0, 'volatility': 0.0}
    usable_signals = 0

    def add_driver(
        *,
        driver_id: str,
        driver_type: str,
        direction: str,
        impact_score: float,
        label_text: str,
        summary: str,
        labels: tuple[str, ...],
        bucket: str,
    ) -> None:
        nonlocal usable_signals
        impact_score = clamp(abs(impact_score), 0.0, 100.0)
        if impact_score <= 0:
            return
        usable_signals += 1
        buckets[bucket] += impact_score * SHORT_PRESSURE_WEIGHTS[driver_type]
        impact = 'high' if impact_score >= 16 else ('medium' if impact_score >= 8 else 'low')
        drivers.append({
            'id': driver_id,
            'type': driver_type,
            'direction': direction,
            'impact': impact,
            'impactScore': round(impact_score, 1),
            'label': label_text,
            'summary': summary,
            'sourceIds': source_id_for(*labels),
        })

    vix_pct = pct(rows, 'VIX')
    vix_level = close(rows, 'VIX')
    if vix_pct is not None:
        if vix_pct > 0:
            add_driver(
                driver_id='vix_speed_up',
                driver_type='coincident',
                direction='volatility_pressure',
                impact_score=min(30.0, vix_pct * 3.0),
                label_text='VIX 변화속도',
                summary='변동성 부담이 단기적으로 커졌습니다.',
                labels=('VIX',),
                bucket='volatility',
            )
        elif vix_pct < 0:
            add_driver(
                driver_id='vix_speed_down',
                driver_type='coincident',
                direction='relief_pressure',
                impact_score=min(20.0, abs(vix_pct) * 1.6),
                label_text='VIX 완화',
                summary='변동성 부담이 일부 완화됐습니다.',
                labels=('VIX',),
                bucket='relief',
            )
    if vix_level is not None and vix_level >= 20:
        add_driver(
            driver_id='vix_level_elevated',
            driver_type='lagging',
            direction='volatility_pressure',
            impact_score=min(18.0, (vix_level - 20.0) * 1.5 + 6.0),
            label_text='VIX 레벨',
            summary='변동성 레벨 자체가 아직 확인 구간입니다.',
            labels=('VIX',),
            bucket='volatility',
        )

    us10y_pct = pct(rows, '미국 10년물')
    if us10y_pct is not None:
        if us10y_pct > 0:
            add_driver(
                driver_id='us10y_up',
                driver_type='coincident',
                direction='risk_pressure',
                impact_score=min(16.0, us10y_pct * 3.2),
                label_text='미국 10년물 상승',
                summary='금리 부담이 성장주와 위험선호를 누를 수 있습니다.',
                labels=('미국 10년물',),
                bucket='risk',
            )
        elif us10y_pct < 0:
            add_driver(
                driver_id='us10y_down',
                driver_type='coincident',
                direction='relief_pressure',
                impact_score=min(12.0, abs(us10y_pct) * 2.2),
                label_text='미국 10년물 완화',
                summary='금리 부담은 일부 완화됐습니다.',
                labels=('미국 10년물',),
                bucket='relief',
            )

    for key, driver_id, label_text, summary in [
        ('DXY', 'dxy_up', '달러 강도', '달러 강세가 글로벌 위험선호에 부담을 줄 수 있습니다.'),
        ('USD/KRW', 'usdkrw_up', '원/달러', '원/달러 상승은 국내시장 부담 요인입니다.'),
    ]:
        value = pct(rows, key)
        if value is None:
            continue
        if value > 0:
            add_driver(
                driver_id=driver_id,
                driver_type='coincident',
                direction='risk_pressure',
                impact_score=min(18.0, value * (5.0 if key == 'USD/KRW' else 4.0)),
                label_text=label_text,
                summary=summary,
                labels=(key,),
                bucket='risk',
            )
        elif value < 0:
            add_driver(
                driver_id=f'{driver_id}_down',
                driver_type='coincident',
                direction='relief_pressure',
                impact_score=min(12.0, abs(value) * 3.0),
                label_text=f'{label_text} 완화',
                summary='달러·환율 부담은 일부 완화됐습니다.',
                labels=(key,),
                bucket='relief',
            )

    us_index_pct = avg([pct_any(rows, 'S&P500', 'S&P500/SPY', 'SPY'), pct_any(rows, 'Nasdaq100', 'Nasdaq100/QQQ', 'QQQ')])
    kr_index_pct = avg([pct(rows, 'KOSPI'), pct(rows, 'KOSDAQ')])
    broad_index_pct = avg([us_index_pct, kr_index_pct])
    if broad_index_pct is not None:
        if broad_index_pct < 0:
            add_driver(
                driver_id='broad_index_fatigue',
                driver_type='lagging',
                direction='risk_pressure',
                impact_score=min(14.0, abs(broad_index_pct) * 5.0),
                label_text='지수 흐름 피로도',
                summary='최근 지수 흐름에는 단기 부담이 남아 있습니다.',
                labels=('S&P500', 'Nasdaq100', 'KOSPI', 'KOSDAQ'),
                bucket='risk',
            )
        elif broad_index_pct > 0:
            add_driver(
                driver_id='broad_index_resilience',
                driver_type='lagging',
                direction='relief_pressure',
                impact_score=min(10.0, broad_index_pct * 3.0),
                label_text='지수 흐름 버팀',
                summary='지수 흐름은 단기 부담을 일부 낮춰줍니다.',
                labels=('S&P500', 'Nasdaq100', 'KOSPI', 'KOSDAQ'),
                bucket='relief',
            )

    semi_pct = avg([pct(rows, 'SOXX'), pct(rows, 'SMH'), pct(rows, '삼성전자'), pct(rows, 'SK하이닉스')])
    if semi_pct is not None:
        if semi_pct < 0:
            add_driver(
                driver_id='semi_bridge_weak',
                driver_type='leading',
                direction='risk_pressure',
                impact_score=min(18.0, abs(semi_pct) * 4.0),
                label_text='반도체 브릿지 약화',
                summary='반도체 선행 신호는 단기 부담 쪽입니다.',
                labels=('SOXX', 'SMH', '삼성전자', 'SK하이닉스'),
                bucket='risk',
            )
        elif semi_pct > 0:
            add_driver(
                driver_id='semi_bridge_support',
                driver_type='leading',
                direction='relief_pressure',
                impact_score=min(12.0, semi_pct * 2.5),
                label_text='반도체 브릿지 지지',
                summary='반도체 선행 신호는 완화 쪽으로 작용합니다.',
                labels=('SOXX', 'SMH', '삼성전자', 'SK하이닉스'),
                bucket='relief',
            )

    if session_type in {'weekend', 'holiday_kr', 'holiday_us', 'pre_market', 'after_market'}:
        add_driver(
            driver_id=f'{session_type}_event_window',
            driver_type='leading',
            direction='volatility_pressure',
            impact_score=8.0 if session_type == 'weekend' else 6.0,
            label_text=session_badge(session_type),
            summary=session_window_summary(session_type),
            labels=(),
            bucket='volatility',
        )

    if usable_signals < 3:
        confidence_score = round(clamp(confidence_from_freshness(signal_freshness, session_type) * 0.55, 0.1, 1.0), 2)
        return {
            'horizon': '1-3d',
            'scope': 'global_bridge',
            'pressureType': 'data_limited',
            'level': 'normal',
            'score': None,
            'label': '단기 압력 확인 중',
            'confidence': {
                'score': confidence_score,
                'level': confidence_level(confidence_score),
                'reasons': ['사용 가능한 단기 압력 신호가 부족합니다.'],
            },
            'drivers': drivers,
            'conflicts': [],
            'weights': SHORT_PRESSURE_WEIGHTS,
            'asOf': as_of or datetime.now(KST).isoformat(timespec='seconds'),
            'disclaimer': SHORT_PRESSURE_DISCLAIMER,
        }

    risk = buckets['risk']
    relief = buckets['relief']
    volatility = buckets['volatility']
    if risk > 12 and relief > 12 and abs(risk - relief) <= 8:
        conflicts.append({
            'id': 'risk_vs_relief',
            'summary': '부담 요인과 완화 요인이 함께 있어 보수적으로 해석합니다.',
            'severity': 'medium',
        })
    if broad_index_pct is not None and broad_index_pct > 0 and (risk + volatility) >= 18:
        conflicts.append({
            'id': 'index_vs_macro',
            'summary': '지수 흐름은 버티지만 금리·달러·변동성 부담은 남아 있습니다.',
            'severity': 'medium',
        })

    if max(risk, relief, volatility) < 8:
        pressure_type = 'neutral'
    elif volatility >= max(risk, relief) + 2:
        pressure_type = 'volatility'
    elif conflicts and abs(risk - relief) <= 8:
        pressure_type = 'mixed'
    elif risk > relief:
        pressure_type = 'risk'
    else:
        pressure_type = 'relief'

    dominant = max(risk, relief, volatility)
    opposite = min(risk, relief) if pressure_type in {'risk', 'relief'} else min(risk, relief, volatility)
    score = clamp(35.0 + dominant * 1.6 - opposite * 0.35 + len(conflicts) * 4.0)
    if pressure_type == 'neutral':
        score = min(score, 49.0)
    level = pressure_level(score)
    confidence_score = confidence_from_freshness(signal_freshness, session_type)
    if conflicts:
        confidence_score = round(max(0.1, confidence_score - 0.12), 2)
    if pressure_type == 'mixed':
        confidence_score = round(max(0.1, confidence_score - 0.08), 2)

    drivers = sorted(drivers, key=lambda item: item.get('impactScore', 0), reverse=True)[:5]
    return {
        'horizon': '1-3d',
        'scope': 'global_bridge',
        'pressureType': pressure_type,
        'level': level,
        'score': round(score),
        'label': pressure_label(pressure_type, level),
        'confidence': {
            'score': confidence_score,
            'level': confidence_level(confidence_score),
            'reasons': ['무료/지연 데이터 기준 단기 압력 v0 룰로 산출했습니다.'] + ([conflicts[0]['summary']] if conflicts else []),
        },
        'drivers': drivers,
        'conflicts': conflicts,
        'weights': SHORT_PRESSURE_WEIGHTS,
        'asOf': as_of or datetime.now(KST).isoformat(timespec='seconds'),
        'disclaimer': SHORT_PRESSURE_DISCLAIMER,
    }


def point_to_row(label: str, point: Any) -> dict:
    if isinstance(point, dict):
        return {
            'label': label,
            'status': point.get('status', 'unknown'),
            'close': point.get('close'),
            'pct': point.get('pct'),
            'reason': point.get('reason'),
            'date': point.get('date'),
            'freshness': point.get('freshness', ''),
        }
    return {
        'label': label,
        'status': getattr(point, 'status', 'unknown'),
        'close': getattr(point, 'close', None),
        'pct': getattr(point, 'pct', None),
        'reason': getattr(point, 'reason', None),
        'date': getattr(point, 'date', None),
        'freshness': getattr(point, 'freshness', ''),
    }


def snapshot_from_points(items: dict[str, Any], *, session_type: str | None = None) -> dict:
    rows = [point_to_row(label, point) for label, point in items.items()]
    invalid = [r for r in rows if r['status'] == 'invalid']
    delayed = [r for r in rows if r['status'] not in {'ok', 'invalid', 'holiday'}]
    ok_count = sum(1 for r in rows if r['status'] == 'ok')
    status = 'invalid' if invalid else ('partial' if delayed or ok_count < 8 else 'ok')
    data = {
        'status': status,
        'ok_count': ok_count,
        'invalid_count': len(invalid),
        'delayed_count': len(delayed),
        'items': rows,
    }
    if session_type:
        data['market_session_type'] = session_type
    return data


def build_scores(snapshot: dict, session_type: str | None = None) -> dict:
    session_type = detect_session(snapshot, session_type)
    profile = WEIGHT_PROFILES[session_type]
    rows = {item['label']: item for item in snapshot['items']}
    as_of = datetime.now(KST).isoformat(timespec='seconds')

    sp500_pct = pct_any(rows, 'S&P500', 'S&P500/SPY', 'SPY')
    nasdaq_pct = pct_any(rows, 'Nasdaq100', 'Nasdaq100/QQQ', 'QQQ')
    smallcap_pct = pct_any(rows, 'Russell 2000', 'Russell 2000/IWM', 'IWM')
    us_index_pct = avg([sp500_pct, nasdaq_pct])
    us_index = component_from_pct(us_index_pct, 8)

    vix_pct = pct(rows, 'VIX')
    vix_level = close(rows, 'VIX')
    volatility = 50
    if vix_pct is not None:
        volatility -= vix_pct * 2
    if vix_level is not None:
        volatility -= max(0, vix_level - 15) * 1.0
    volatility = clamp(volatility)

    macro = 50
    us10y_pct = pct(rows, '미국 10년물')
    dxy_pct = pct(rows, 'DXY')
    if us10y_pct is not None:
        macro -= us10y_pct * 3
    if dxy_pct is not None:
        macro -= dxy_pct * 2
    macro = clamp(macro)

    semi_us_pct = avg([pct(rows, 'SOXX'), pct(rows, 'SMH')])
    sector = component_from_pct(semi_us_pct, 7)
    sentiment = 50  # B1 placeholder until source is decided

    us_components = {
        'index_momentum': us_index,
        'volatility': volatility,
        'macro': macro,
        'sector': sector,
        'sentiment_placeholder': sentiment,
    }
    us_raw_score = weighted_average(us_components, profile['us'], {'sector': US_SECTOR_DELTA_CAP})
    us_score, us_guardrail = us_broad_market_guardrail(us_raw_score, sp500_pct, nasdaq_pct, smallcap_pct)

    kr_index_pct = avg([pct(rows, 'KOSPI'), pct(rows, 'KOSDAQ')])
    kr_index = component_from_pct(kr_index_pct, 5)

    supply = 50  # B1 placeholder: foreign/institution supply unavailable

    usdkrw_pct = pct(rows, 'USD/KRW')
    usdkrw_level = close(rows, 'USD/KRW')
    currency = 50
    if usdkrw_pct is not None:
        currency -= usdkrw_pct * 8
    if usdkrw_level is not None:
        currency -= max(0, usdkrw_level - 1400) / 10
    currency = clamp(currency)

    domestic_semi_pct = avg([pct(rows, '삼성전자'), pct(rows, 'SK하이닉스'), pct(rows, 'SOXX'), pct(rows, 'SMH')])
    industry = component_from_pct(domestic_semi_pct, 4)
    domestic_news = 50  # B1 placeholder until source is decided

    kr_components = {
        'index_momentum': kr_index,
        'supply_placeholder': supply,
        'currency': currency,
        'industry': industry,
        'domestic_news_placeholder': domestic_news,
    }
    kr_score = weighted_average(kr_components, profile['kr'], {'industry': KR_INDUSTRY_DELTA_CAP})

    signal_freshness = {
        'us_price_momentum': freshness_for_status(rows.get('S&P500', {}).get('status'), session_type),
        'kr_price_momentum': freshness_for_status(rows.get('KOSPI', {}).get('status'), session_type),
        'currency': freshness_for_status(rows.get('USD/KRW', {}).get('status'), session_type, price_like=False),
        'volatility': freshness_for_status(rows.get('VIX', {}).get('status'), session_type, price_like=False),
        'macro_index': freshness_for_status(rows.get('미국 10년물', {}).get('status'), session_type, price_like=False),
        'news_sentiment': 'live' if session_type in {'holiday_kr', 'holiday_us', 'weekend', 'pre_market', 'after_market'} else 'stale_1d',
    }
    score_confidence = confidence_from_freshness(signal_freshness, session_type)
    short_pressure = build_short_pressure(rows, session_type, signal_freshness, as_of)

    bridge = [
        {
            'key': 'usd_krw',
            'name': '원/달러',
            'value': close(rows, 'USD/KRW'),
            'changePct': pct(rows, 'USD/KRW'),
            'impact': impact_from_pct(pct(rows, 'USD/KRW'), '국내 수급 부담', '환율 부담 완화'),
            'status': rows.get('USD/KRW', {}).get('status', 'unknown'),
        },
        {
            'key': 'vix',
            'name': 'VIX',
            'value': close(rows, 'VIX'),
            'changePct': pct(rows, 'VIX'),
            'impact': impact_from_pct(pct(rows, 'VIX'), '변동성 경계', '변동성 완화'),
            'status': rows.get('VIX', {}).get('status', 'unknown'),
        },
        {
            'key': 'us10y',
            'name': '미국 10년물',
            'value': close(rows, '미국 10년물'),
            'changePct': pct(rows, '미국 10년물'),
            'impact': impact_from_pct(pct(rows, '미국 10년물'), '성장주·기술주 부담', '금리 부담 완화'),
            'status': rows.get('미국 10년물', {}).get('status', 'unknown'),
        },
    ]

    why = [
        f"미국은 지수·반도체 흐름과 VIX/금리 신호 때문에 {label(us_score)}입니다.",
        f"국내는 지수 흐름과 원/달러 부담 때문에 {label(kr_score)}입니다.",
        '두 시장 차이는 환율·변동성·금리 브릿지 신호를 함께 확인하는 구간입니다.',
    ]

    return {
        'asOf': as_of,
        'validation': {
            'status': snapshot['status'],
            'ok_count': snapshot['ok_count'],
            'invalid_count': snapshot['invalid_count'],
            'delayed_count': snapshot['delayed_count'],
        },
        'market_context': {
            'market_session_type': session_type,
            'weight_profile': f'{session_type}_v1',
            'score_confidence': score_confidence,
            'signal_freshness': signal_freshness,
            'b1_guards': {
                'contribution_delta_caps': {
                    'us_sector': US_SECTOR_DELTA_CAP,
                    'kr_industry': KR_INDUSTRY_DELTA_CAP,
                },
                'component_scores_preserved': True,
                'placeholders': ['sentiment_placeholder', 'supply_placeholder', 'domestic_news_placeholder'],
                'us_broad_market_guardrail': us_guardrail,
                'short_pressure': {
                    'separate_from_temperatures': True,
                    'horizon': '1-3d',
                    'weights': SHORT_PRESSURE_WEIGHTS,
                },
            },
        },
        'us_temperature': {
            'score': round(us_score),
            'rawScore': round(us_raw_score, 1),
            'label': label(us_score),
            'sessionBadge': session_badge(session_type),
            'scoreConfidence': score_confidence,
            'components': {k: round(v, 1) for k, v in us_components.items()},
            'weights': profile['us'],
        },
        'kr_temperature': {
            'score': round(kr_score),
            'label': label(kr_score),
            'sessionBadge': session_badge(session_type),
            'scoreConfidence': score_confidence,
            'components': {k: round(v, 1) for k, v in kr_components.items()},
            'weights': profile['kr'],
        },
        'short_pressure': short_pressure,
        'bridge_signals': bridge,
        'why_different': why,
        'disclaimer': '이 브리핑은 시장 환경 정보이며 투자 권유가 아닙니다.',
    }
