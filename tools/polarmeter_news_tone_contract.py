#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone

from polarmeter_news_rss_probe import (
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
