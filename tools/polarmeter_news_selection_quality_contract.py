#!/usr/bin/env python3
"""Offline contract: generated Korean lines keep source facts; critical stays rare."""
from __future__ import annotations

from polarmeter_news_rss_probe import (
    CRITICAL_ITEM_LIMIT,
    cap_critical_items,
    collapse_near_duplicate_headlines,
    koreanize_english_headline,
    refine_selection_quality,
    strip_headline_tail,
    translation_keeps_direction_place_and_event,
)


def keeps(source: str, source_name: str = '') -> bool:
    translated = koreanize_english_headline(source)
    return bool(translated) and translation_keeps_direction_place_and_event(source, translated, source_name)


def main() -> None:
    # 2026-11-03 live cards whose generated line reversed, relocated or invented the event.
    assert not keeps('India bonds end tad higher on softer oil', 'Business Recorder')
    assert not keeps('Year-end inflation challenges as CPI rises 4.57 per cent in 10 months', 'vietnamnews.vn')
    assert not keeps('Felix Nikolas Prehn: The Prehn Institute on cash savings and the CPI', 'USA Today')
    assert not keeps('Syria says it foiled Hezbollah-linked plot to attack Homs oil refinery', 'Türkiye Today')
    assert not keeps('Saudi Aramco Profit Jumps Despite Damaging Attacks on Pipelines and Refineries', 'NYT')
    # Faithful summaries stay.
    assert keeps('Flames at Russian oil refinery following Ukrainian drone attack', 'Fakti')
    assert keeps('Stock Market Today: Dow Slides On Election Day; Apple, Palantir Dive On Earnings', 'Yahoo Finance RSS')
    assert keeps('Oil rises as Iran rules out sanctions relief', 'Reuters')

    assert strip_headline_tail('이란 대통령 "조건 맞으면 해협 재개방" - 머니투데이', '머니투데이') == '이란 대통령 "조건 맞으면 해협 재개방"'
    assert strip_headline_tail('미국, 게슘섬 타격…방어 조치 : 국제', '재경일보') == '미국, 게슘섬 타격…방어 조치'

    base = {'critical': True, 'priorityTier': 'CRITICAL', 'qualityScore': 0.9, 'publishedAt': '2026-11-03T10:00:00Z'}
    off_topic = refine_selection_quality({**base, 'headline': '"러, 외국인에 일자리 준다 속여 전쟁 투입...식별번호 문신도"', 'sourceName': 'YTN'})
    assert off_topic['critical'] is False and off_topic['selectionNote'] == 'no_market_link'
    promo = refine_selection_quality({**base, 'headline': '나스닥 -5.3% 하락 속, 이 AI 전략들은 +9.3% 급등', 'sourceName': 'Investing.com 한국어'})
    assert promo['critical'] is False and promo['selectionNote'] == 'promotional_headline'
    low_trust = refine_selection_quality({**base, 'headline': '이란은 이틀 안에 호르무즈 해협을 재개방할 것을 제안했다.', 'sourceName': 'Vietnam.vn'})
    assert low_trust['critical'] is False and low_trust['qualityScore'] < 0.9
    market = refine_selection_quality({**base, 'headline': "'AI 거품론'에 발목 잡힌 증시…코스피 3%대 급락", 'sourceName': '연합뉴스TV'})
    assert market['critical'] is True

    many = [{**base, 'headline': f'코스피 급락 사례 {index}'} for index in range(CRITICAL_ITEM_LIMIT + 4)]
    capped = cap_critical_items(many, CRITICAL_ITEM_LIMIT)
    assert len(capped) == len(many) and sum(item['critical'] for item in capped) == CRITICAL_ITEM_LIMIT

    same = {**base, 'critical': False}
    reports = [
        {**same, 'headline': '한은 기준금리 동결 결정 물가 경기 부담 함께 고려', 'sourceName': 'A'},
        {**same, 'headline': '한은 기준금리 동결 결정…물가 경기 부담 함께 고려했다', 'sourceName': 'B'},
        {**same, 'headline': '한은 기준금리 0.25%p 인상 결정 물가 경기 부담 고려', 'sourceName': 'C'},
    ]
    kept, dropped = collapse_near_duplicate_headlines(reports)
    assert dropped == 1 and len(kept) == 2 and kept[0]['relatedReportCount'] == 2, kept
    # Opposite direction or a different trading stage is a new update, not a corroborating report.
    stages = [
        {**same, 'headline': '코스피 상승 출발 외국인 수급 반도체 중심 증시 흐름 주목', 'sourceName': 'A'},
        {**same, 'headline': '코스피 하락 마감 외국인 수급 반도체 중심 증시 흐름 주목', 'sourceName': 'B'},
        {**same, 'headline': '코스피 상승 마감 외국인 수급 반도체 중심 증시 흐름 주목', 'sourceName': 'C'},
    ]
    kept, dropped = collapse_near_duplicate_headlines(stages)
    assert dropped == 0 and len(kept) == 3, kept
    print('PASS news selection quality: source-faithful translations, rare critical badge, one card per report')


if __name__ == '__main__':
    main()
