#!/usr/bin/env python3
"""Contract for the P1 items left by the 2026-11-21 quality check (out/qa-20261121, 형 "응, 이어서 고쳐").

N6  a machine-translated Vietnamese digest (Laodong) is not a news source.
N7  energy template titles keep the country and a reported output cut, never read "원유 공급 공격", end on a phrase the
    app does not cut ("…유가 반응 확인"), and a town's natural-gas service story is not an oil supply incident.
N10 one event is one card group: a central-bank decision, a jobs release, Korea's export record, Gaza, one energy
    incident, and one inflation release whatever its surprise word. An index close that names the event stays an
    index card.
Also: "2026 gains erased" and a Fed official arguing for a hike are burdens, not relief (live cards of 11-21).
P1-1 a Monday forecast issued on Saturday says so on the card and in the 08:05 push.
"""
import polarmeter_morning_forecast as mf
import polarmeter_news_rss_probe as probe
import polarmeter_push_outbox as po


def key(headline: str, **extra) -> str:
    return probe.issue_cluster_key({'headline': headline, 'originalHeadline': headline,
                                    'publishedAt': '2026-11-20T01:00:00Z', **extra})


def check_sources() -> None:
    assert probe.LOW_TRUST_NEWS_SOURCES.search('Laodong.vn')
    assert any('laodong' in hint for hint in probe.EXCLUDED_SOURCE_HINTS)


def check_energy_titles() -> None:
    cut = probe.koreanize_english_headline('Attacks Cut Saudi Oil Output and East-West Pipeline Flow')
    assert cut and '사우디' in cut and '생산 감소' in cut and '동서 송유관' in cut, cut
    hormuz = probe.koreanize_english_headline('Attack on oil supply near Hormuz strait')
    assert hormuz and '원유 공급 공격' not in hormuz, hormuz
    for title in (cut, hormuz, probe.koreanize_english_headline('Saudi oil pipeline attacked by drones')):
        assert title and title.endswith('유가 반응 확인') and '영향 확인' not in title, title
    town = 'Elizabethtown residents no longer need to conserve natural gas after pipeline damage'
    assert probe.is_local_non_oil_infrastructure(town)
    assert probe.koreanize_english_headline(town) is None
    assert not probe.is_energy_supply_headline(town)
    assert not probe.local_gas_utility_incident('European natural gas prices jump after pipeline outage')
    assert probe.is_energy_supply_headline('Russian oil pipeline attack halts exports')


def check_event_families() -> None:
    bok = {key(h) for h in ('수출 1조달러 시대…한은 추가 인상 확실시', '올해 마지막 금통위⋯3.25% 인상에 무게',
                            "도이체 '한국은행 11월 25bp 인상' 전망")}
    assert bok == {'issue:family:cb:bok'}, bok
    assert key('코스피, 한은 금리 인상 우려에 2% 하락 마감') == 'issue:kr_market_flow', 'an index close stays an index card'
    au = {key('호주 10월 실업률 4.9%…호주달러 급락'), key('Australia unemployment rate rises to 4.9% in October')}
    assert au == {'issue:family:labour:au'}, au
    assert key('Jobs for us: hiring picks up at small firms') != 'issue:family:labour:us', 'the word "us" is not the US'
    exports = {key('한국 수출 사상 첫 1조달러 돌파'), key('올해 수출 1조 달러…역대 최대')}
    assert exports == {'issue:family:kr_exports'}, exports
    gaza = {key('가자 휴전 2단계 협상 난항'), key('Israel strikes Gaza despite ceasefire')}
    assert gaza == {'issue:family:gaza'}, gaza
    saudi = {key('Drone attack hits Saudi oil pipeline'), key('동서 송유관 메디나 구간 피격…사우디 에너지 인프라 잇단 피격')}
    assert saudi == {'issue:family:energy:sa'}, saudi
    assert probe.issue_limit('issue:family:cb:bok', 3) == 2 and probe.issue_limit('issue:family:labour:au', 3) == 1
    assert probe.issue_limit('issue:hormuz_security', 3) == 2 and probe.issue_limit('issue:other', 3) == 3
    crash = {'displayHeadline': '나스닥 5.28% 폭락하며 올해 상승분 전액 반납', 'publishedAt': '2026-11-20T22:00:00Z', 'marketImpactScore': 90}
    bridge = {'displayHeadline': "美 나스닥 5%대 하락에…韓 증시 '충격파' 우려", 'publishedAt': '2026-11-20T23:00:00Z', 'marketImpactScore': 94}
    assert probe.market_wrap_representative([crash, bridge], 'issue:us_index_market_wrap') is crash, 'a US close report represents the US close'
    assert key('나스닥, 기술주 매도에 5.3% 폭락…다우·S&P500도 조정 진입') == 'issue:us_index_market_wrap'
    above, below = key('일본 10월 CPI 3.1% 상승…예상 상회'), key('일본 10월 CPI 3.1%…예상 하회')
    assert above == below, (above, below)
    assert key('미국 8월 근원 PCE 3.0% 상승…예상 하회') != key('미국 8월 PCE 3.4% 상승…예상 상회'), 'core and headline stay apart'


def check_reversed_translations() -> None:
    erased = probe.english_market_context_translation(
        'Nasdaq stock market today: 2026 gains erased as Wall Street warns of further losses')
    assert erased == '나스닥 올해 상승분 반납은 미국장 부담 신호', erased
    for hawk in ("Fed's Cook says waiting to raise rates risks falling behind the curve on inflation",
                 "Cook Says Fed 'Does Not Have the Luxury' of Waiting as Inflation Runs Hot"):
        shown = probe.english_market_context_translation(hawk)
        assert shown and '완화' not in shown and '줄었' not in shown and probe.market_burden_tone(shown) == 'negative', shown


def check_weekend_forecast_note() -> None:
    saturday = {'targetSession': '2026-11-23', 'issuedAt': '2026-11-20T21:57:28Z'}
    note = mf.issued_note_copy(saturday)
    assert note and note['ko'] == '토요일 아침 기준(주말 소식 반영 전)' and note['shortKo'] == '토요일 아침 기준', note
    assert mf.issued_note_copy({'targetSession': '2026-11-24', 'issuedAt': '2026-11-23T21:50:00Z'}) is None, 'Tuesday'
    assert mf.issued_note_copy({'targetSession': '2026-11-23', 'issuedAt': None}) is None
    current = {
        **saturday,
        'predicted': {'openCall': 'down'},
        'copy': {'title': {'ko': '월요일 한국장, 크게 밀려서 시작할 것으로 보여요', 'en': 'Monday: Korea looks set to open sharply lower'},
                 'openStat': {'ko': '−2.5% 안팎', 'en': 'around −2.5%'}},
    }
    scorecard = {'live': {'calls': 8, 'openHits': 6}}
    texts = po.morning_copy(current, scorecard)
    assert texts, 'the push still goes out'
    body = texts[1]
    assert '토요일 아침 기준' in body['ko'] and 'as of Sat morning' in body['en'], body
    assert len(body['ko']) <= po.LIMIT_BODY_KO, body
    weekday = {**current, 'targetSession': '2026-11-24', 'issuedAt': '2026-11-23T21:50:00Z'}
    assert '기준' not in po.morning_copy(weekday, scorecard)[1]['ko']


def main() -> None:
    check_sources()
    check_energy_titles()
    check_event_families()
    check_reversed_translations()
    check_weekend_forecast_note()
    print('PASS news P1 2026-11-21: low-trust digest, energy titles, one card group per event, erased gains and Fed hike talk, weekend forecast note')


if __name__ == '__main__':
    main()
