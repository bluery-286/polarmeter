"""Offline actual-source regressions: no fabricated price direction or repeated event cards."""
from datetime import datetime, timezone, timedelta
from polarmeter_news_rss_probe import normalize_items, energy_supply_state, tanker_incident_key, critical_market_event, koreanize_english_headline, classify_relevance, is_local_non_oil_infrastructure, issue_cluster_key
from polarmeter_news_fact_semantics import has_divergent_index_moves


def main():
    now = datetime.now(timezone.utc).isoformat()
    headlines = [
        '호르무즈 해협서 또 유조선 피격…이달 들어 네 번째',
        '"호르무즈 해협서 또 유조선 피격"...이달에만 벌써 4차례',
        '호르무즈 해협서 유조선 피격…이번 달 4번째',
        '"호르무즈 해협서 또 유조선 피격"…이달만 최소 4차례',
    ]
    def rows(titles):
        return [{'headline': h, 'sourceName': f'QA{i}', 'publishedAt': now,
                 'url': f'https://example.com/source-event/{i}'} for i, h in enumerate(titles)]
    def normalize(titles):
        return normalize_items([{'label': 'QA', 'items': rows(titles)}], 30)[0]
    key = tanker_incident_key(headlines[0], now)
    assert key is not None
    for h in headlines:
        assert energy_supply_state(h) == 'active', h
        assert tanker_incident_key(h, now) == key, h
    assert len(normalize(headlines)) == 1, 'same source incident must appear once'
    for other in [
        '호르무즈 해협서 유조선 피격…이번 달 5번째',
        '홍해에서 유조선 피격…이번 달 4번째',
        '호르무즈 유조선 봉쇄…이번 달 4번째',
        '호르무즈 해협 유조선 피격 보도 부인…이번 달 4번째',
        '호르무즈 해협 통항 재개·원유 공급 재개…이번 달 4번째',
        '호르무즈 해협서 유조선 피격…지난달 4번째',
        '호르무즈 해협서 유조선 피격…이번 달 4번째, 10월 6일',
        '호르무즈 해협서 다른 유조선 피격…이달 들어 네 번째',
        "호르무즈 해협서 유조선 '알파'호 피격…이번 달 4번째",
    ]:
        assert tanker_incident_key(other, now) != key, other
        assert len(normalize([headlines[0], other])) == 2, other
    # The short transit-only title is filtered by the existing relevance gate,
    # independently of deduplication. Check identity directly; the full supply
    # reopening above must survive the entire pipeline alongside the attack.
    assert tanker_incident_key('호르무즈 해협 통항 재개…이번 달 4번째', now) != key
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert tanker_incident_key(headlines[0], old) != key
    assert tanker_incident_key(headlines[0], None) is None
    for title in [
        '중동 원유수출 회복세에 찬물…이란 호르무즈 공격 다시 증가',
        '중동 수출 98% 회복에도 유가 90달러…호르무즈 비용',
    ]:
        out = normalize([title])
        assert len(out) == 1, title
        assert out[0]['displayHeadline'] == title, out
        assert '유가 하락' not in out[0]['whyImportant'], out
    genuine = normalize(['유가 3% 하락…원유 공급 재개'])
    assert len(genuine) == 1 and '하락' in genuine[0]['displayHeadline'], genuine
    recovery = '사우디 동서 송유관 하루 580만배럴 수송 회복…드론 공격 닷새 만에 재가동'
    assert energy_supply_state(recovery) == 'recovery'
    restored = normalize([recovery])
    assert len(restored) == 1 and restored[0]['impactTone'] == 'neutral', restored
    assert '회복' in restored[0]['whyImportant'] and '공급 차질 우려는' not in restored[0]['whyImportant']
    for title, expected in [
        ('사우디 송유관 재가동 예정', 'unknown'),
        ('사우디 송유관 재가동 기대', 'unknown'),
        ('사우디 송유관 수송 회복 가능성', 'unknown'),
        ('사우디 송유관 재가동 하지 않아', 'unknown'),
        ('사우디 송유관 수송 회복 불투명', 'unknown'),
        ('사우디 송유관 재가동했지만 공급 차질 지속', 'mixed'),
        ('사우디 송유관 재가동·공급 차질 지속', 'mixed'),
        ('사우디 송유관 재가동…공격 계속', 'mixed'),
    ]:
        assert energy_supply_state(title) == expected, (title, energy_supply_state(title))
    local = 'Kyiv Restores Northern Bridge After Attack, Repairs Structure, Roadway, and Heating Pipeline; Backup Crossings Planned'
    assert energy_supply_state(local) == 'unknown'
    assert not critical_market_event(local)[0]
    assert koreanize_english_headline(local) is None
    assert classify_relevance(local, 'QA', now)[1] == 'LOCAL_UTILITY_NOT_MARKET_TEMPERATURE'
    assert normalize([local]) == [], 'local heating pipe must not become an oil attack'
    for utility in (
        'HMWSSB saves 18 MGD of drinking water during Krishna pipeline shutdown',
        'Krishna pipeline shutdown disrupts municipal drinking water supply',
        'Potable-water supply restored after pipeline repairs',
        'Pipeline maintenance delays wastewater treatment',
    ):
        assert is_local_non_oil_infrastructure(utility), utility
        assert energy_supply_state(utility) == 'unknown', utility
        assert not critical_market_event(utility)[0], utility
        assert koreanize_english_headline(utility) is None, utility
        assert normalize([utility]) == [], utility
    for market in (
        'Crude pipeline shutdown contaminates drinking water',
        'Drinking water pipeline repair affects global gas supply',
    ):
        assert not is_local_non_oil_infrastructure(market), market
    # Explicit crude/national-gas reports must remain eligible, even when a
    # local heating pipe is mentioned in the same headline.
    assert normalize(['Crude pipeline shutdown after attack disrupts heating pipeline'])
    for market in ('global gas supply', 'gas prices', 'European energy supply'):
        assert not is_local_non_oil_infrastructure(f'Heating pipeline damage affects {market}')
    expected_recovery = normalize(['사우디 송유관 재가동 기대에 유가 3% 하락'])
    assert len(expected_recovery) == 1 and expected_recovery[0]['impactTone'] == 'positive', expected_recovery
    rising = 'Why is US Stock Market Up Today? Dow Jones Gains 359 Points, S&P 500 Rises 0.86%, Nasdaq Climbs 0.83% as Oil Prices Fall and Treasury Yields Ease; Check What Investors Should Know'
    assert not has_divergent_index_moves(rising), 'oil fall must not become Nasdaq fall'
    rising_card = normalize([rising])
    assert len(rising_card) == 1 and rising_card[0]['impactTone'] == 'positive', rising_card
    assert '지수 상승' in rising_card[0]['displayHeadline'] and '국채 금리 하락' in rising_card[0]['displayHeadline']
    assert '엇갈' not in rising_card[0]['whyImportant']
    divergent = '코스피 6941.39로 7000선 내줘...코스닥은 3% 가까이 상승'
    assert has_divergent_index_moves(divergent)
    assert normalize([divergent])[0]['impactTone'] == 'neutral'
    related = [
        '후티, 사우디 공항·정유시설 공격…현대차 직원 숙소 인근 불길',
        '후티, 사우디 공항·정유시설 공습…韓기업 진출 라빅도 피격',
        '거센 반격 후티, 사우디 공항·정유시설 타격…한국 기업 인근도 피격',
        '후티, 사우디 공항·정유시설 공격⋯韓 기업 진출 라빅서 화재',
        '후티, 사우디 공항·정유시설 공격…선원 12명 부상',
        '후티, 사우디 공항·정유시설 공격…선원 10명 치료 이송',
    ]
    grouped = normalize(related)
    assert len(grouped) == 3, grouped
    preserved = next(c for c in grouped if c.get('relatedReports'))
    assert preserved['relatedReportCount'] == 6
    assert preserved['issueGrouping'] == 'related_topic_not_confirmed_same_event'
    assert {r['headline'] for r in preserved['relatedReports']} == set(related)
    assert all(c.get('relatedReportCount') == 6 for c in grouped)
    assert any('12명 부상' in r['headline'] for r in preserved['relatedReports'])
    assert any('10명 치료 이송' in r['headline'] for r in preserved['relatedReports'])
    assert issue_cluster_key({'headline': related[0], 'publishedAt': now}) == issue_cluster_key({
        'headline': related[1], 'publishedAt': (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(),
    }), 'UTC midnight must not split a rolling-window topic group'
    # Recovery and denial are never merged into the attack-topic bucket.
    assert len(normalize(related[:3] + ['후티 사우디 정유시설 공격 부인', '사우디 정유시설 피격 후 복구 완료'])) == 5
    print('PASS server source/event contract')


if __name__ == '__main__':
    main()
