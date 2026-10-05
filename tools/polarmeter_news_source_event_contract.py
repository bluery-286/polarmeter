"""Offline actual-source regressions: no fabricated price direction or repeated event cards."""
from datetime import datetime, timezone, timedelta
from polarmeter_news_rss_probe import normalize_items, energy_supply_state, tanker_incident_key


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
    print('PASS server source/event contract')


if __name__ == '__main__':
    main()
