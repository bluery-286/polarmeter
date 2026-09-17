"""Offline regression: event collection is separate from direction certainty."""
from polarmeter_news_rss_probe import critical_market_event, DEFAULT_FEEDS, energy_supply_risk, energy_supply_state, koreanize_english_headline
from polarmeter_news_rss_probe import normalize_items, classify_relevance
from datetime import datetime, timezone, timedelta


def feed_item(headline: str, suffix: str, published_at: str) -> dict:
    return {
        'headline': headline,
        'sourceName': 'Energy coverage contract',
        'publishedAt': published_at,
        'url': f'https://example.com/energy-coverage/{suffix}',
    }

def main():
    for headline in ('원유 송유관 피격', '정유시설 공급 차질', 'Crude pipeline offline after attack', '해협 봉쇄로 항로 중단'):
        assert critical_market_event(headline) == (True, 'energy_transport_disruption'), headline
    assert critical_market_event('사우디 송유관 파손…복구 작업 중') == (True, 'energy_transport_disruption')
    assert not critical_market_event('사우디 정유시설 damage report')[0]
    for headline in ('송유관 신설 사업 설명', 'Pipeline maintenance training course'):
        assert not critical_market_event(headline)[0], headline
    for suffix in ('kr', 'us'):
        feed = next(f for f in DEFAULT_FEEDS if f['sourceId'] == f'rss:google-news:energy-infrastructure-{suffix}')
        assert feed['url'].startswith('https://news.google.com/rss/search?')
    assert energy_supply_risk('Oil prices surge as Saudi pipeline shutdown continues')
    assert not energy_supply_risk('Pipeline restored after attack')
    assert '사우디 송유관 가동 중단' in koreanize_english_headline('Oil prices surge as Saudi pipeline shutdown continues')
    for title, state in [
        ('원유 공급 차질 우려 완화', 'recovery'),
        ('사우디 송유관 복구 완료·공급 재개', 'recovery'),
        ('원유 공급 차질 재개', 'active'),
        ('피격 사우디 동서송유관 펌프장 2곳 파손‥복구 불투명', 'active'),
        ('송유관 공격 부인', 'denied'),
        ('송유관 피격 사실무근', 'denied'),
        ('송유관 공격 부인에도 공급 차질 지속', 'mixed'),
        ('송유관 피격 이후 일부 가동 재개', 'mixed'),
        ('송유관 공급 차질 지속하지만 일부 가동 재개', 'mixed'),
        ('송유관 피격 후 복구 완료', 'recovery'),
        ('Pipeline restored after attack', 'recovery'),
        ('송유관 복구 작업 중', 'unknown'),
        ('송유관 복구 진행 중', 'unknown'),
        ('송유관 복구 예정', 'unknown'),
        ('송유관 복구 시도', 'unknown'),
        ('송유관 파손 복구 작업 중', 'active'),
        ('송유관 가동 재개 불투명', 'unknown'),
        ('송유관 공급 재개 지연', 'unknown'),
        ('송유관 복구 완료 예정', 'unknown'),
        ('송유관 피격 후 가동 재개 불투명', 'unknown'),
        ('송유관 피격 후 복구 완료 예정', 'unknown'),
        ('Oil companies struck a deal', 'unknown'),
    ]:
        assert energy_supply_state(title) == state, title
    for qualifier in ('불투명', '어려움', '지연', '난항', '미완료', '불확실', '불가', '실패'):
        title = f'피격 사우디 동서송유관 펌프장 2곳 파손…복구 {qualifier}'
        assert energy_supply_state(title) == 'active', title
    assert energy_supply_state('Crude oil supply disruption eases') == 'recovery'
    now = datetime.now(timezone.utc)
    for title in ('송유관 공격', '정유시설 공급 차질', 'Pipeline offline after attack'):
        items, report = normalize_items([{'label': 'Reuters', 'items': [{
            'headline': title, 'publishedAt': now.isoformat(),
            'url': 'https://www.reuters.com/world/energy-test',
        }]}], 30)
        assert len(items) == 1, (title, report)
        assert items[0]['impactTone'] == 'negative', items
        assert items[0]['critical'] is True, items
        assert classify_relevance(title, 'Reuters', (now - timedelta(hours=25)).isoformat())[0] is None
    published_at = now.isoformat()
    active_headline = '피격 사우디 동서송유관 펌프장 2곳 파손‥복구 불투명'
    same_event_typography = [
        active_headline,
        '피격 사우디 동서 송유관 펌프장 2곳 파손…복구 불투명',
        '‘피격’ 사우디 동서 송유관 펌프장 2곳 파손…복구 불투명',
    ]
    distinct_active_events = [
        '피격 UAE 동서송유관 펌프장 2곳 파손…복구 불투명',
        '피격 사우디 동서송유관 저장탱크 2곳 파손…복구 불투명',
        '피격 사우디 동서송유관 펌프장 3곳 파손…복구 불투명',
        '17일 피격 사우디 동서송유관 펌프장 2곳 파손…복구 불투명',
        '18일 피격 사우디 동서송유관 펌프장 2곳 파손…복구 불투명',
    ]
    items, report = normalize_items([{
        'label': 'Energy coverage contract',
        'items': [
            feed_item(title, f'variant-{index}', published_at)
            for index, title in enumerate(same_event_typography + distinct_active_events)
        ],
    }], 20)
    assert report['filteredReasons'].get('DUPLICATE_DISPLAY_HEADLINE') == 2, report
    assert len(items) == 1 + len(distinct_active_events), (items, report)
    assert all(item['impactTone'] == 'negative' and item['critical'] for item in items), items
    assert {item['headline'] for item in items} == {active_headline, *distinct_active_events}, items
    expected_recovery = '사우디 송유관 복구 기대에 국제유가 이틀째 하락…WTI 102달러선'
    items, report = normalize_items([{
        'label': 'Energy coverage contract',
        'items': [feed_item(expected_recovery, 'expected-recovery', published_at)],
    }], 1)
    assert len(items) == 1 and items[0]['impactTone'] != 'negative', (items, report)
    pipeline_damage_headline = '사우디 송유관 파손…복구 작업 중'
    items, report = normalize_items([{
        'label': 'Energy coverage contract',
        'items': [feed_item(pipeline_damage_headline, 'pipeline-damage', published_at)],
    }], 1)
    assert len(items) == 1, report
    assert items[0]['critical'] is True and items[0]['impactTone'] == 'negative', items
    conservative_cases = {
        '송유관 피격 사실무근': '사실무근',
        '송유관 피격 후 복구 작업 중': '진행·예정·시도 단계',
        '송유관 피격 이후 일부 가동 재개': '실제 시설 가동',
        '송유관 피격 후 복구 완료': '실제 시설 가동',
        '송유관 피격 후 가동 재개 불투명': '진행·예정·시도 단계',
        '송유관 피격 후 복구 완료 예정': '진행·예정·시도 단계',
    }
    for index, (title, expected_why) in enumerate(conservative_cases.items()):
        items, report = normalize_items([{
            'label': 'Energy coverage contract',
            'items': [feed_item(title, f'conservative-{index}', published_at)],
        }], 1)
        assert len(items) == 1, (title, report)
        assert items[0]['impactTone'] == 'neutral', (title, items)
        assert expected_why in items[0]['whyImportant'], (title, items)
    print('PASS energy event coverage including full normalization and freshness gates (no network)')

if __name__ == '__main__':
    main()
