#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone

from polarmeter_news_rss_probe import (
    cause_aware_display_headline,
    explicit_oil_price_directions,
    market_burden_tone,
    oil_relief_signal,
    oil_burden_signal,
    tone_aligned_why,
    mixed_inflation_relief_rate_burden_signal,
    news_tone_explanation_conflict,
    normalize_items,
)


def feed_item(headline: str, suffix: str) -> dict:
    return {
        'label': 'Tone contract',
        'items': [{
            'headline': headline,
            'sourceName': 'Tone contract',
            'publishedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'url': f'https://example.com/{suffix}',
        }],
    }


def main() -> int:
    for headline in (
        '[투자 노하우] 뉴욕증시, 중동 정세 격화에 다우 1.2%↓…유가 상승',
        '다우 0.8% 하락…유가 2% 상승',
        'Nasdaq falls; oil prices rise',
    ):
        assert explicit_oil_price_directions(headline) == {'rise'}, headline
        assert not oil_relief_signal(headline), headline
        assert oil_burden_signal(headline), headline
        visible = cause_aware_display_headline(headline, headline)
        assert not visible.startswith('유가 부담 완화'), visible
        tone = market_burden_tone(headline)
        assert tone == 'negative', (headline, tone)
        why = tone_aligned_why(headline, headline, '', tone)
        assert '유가가 오르면' in why and '유가 하락' not in why, why
    assert oil_relief_signal('유가 3% 하락…나스닥 상승')
    assert not oil_burden_signal('유가 3% 하락…나스닥 상승')
    mixed = '미국증시, 마이크로소프트 15% 급등…물가 둔화에도 금리 부담'
    assert mixed_inflation_relief_rate_burden_signal(mixed)
    items, report = normalize_items([feed_item(mixed, 'mixed')], 1)
    assert len(items) == 1, report
    assert items[0]['impactTone'] == 'neutral'
    assert items[0]['whyImportant'] == (
        '주가 상승과 물가 둔화가 보이지만 금리 부담도 남아 시장 온도에는 중립 신호로 봅니다.'
    )
    assert not news_tone_explanation_conflict(items[0]['impactTone'], items[0]['whyImportant'])
    assert news_tone_explanation_conflict(
        'negative',
        '물가 부담이 낮아지면 금리 압박이 줄어 시장 부담을 덜 수 있습니다.',
    )
    print('PASS — mixed news is neutral and contradictory cards are quarantined')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
