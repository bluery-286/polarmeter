#!/usr/bin/env python3
"""Contract for the items left after the 2026-11-21 P1 fixes (형 "남은 것도 이어서 고쳐", 2026-11-22).

Live cards of 11-22 01:26 KST that read the source backwards or said nothing:
  "…ended the trading session in correction territory, while the Nasdaq wiped out all its year-to-date gains"
      was '상승 흐름은 미국장 완화 신호'                    -> 조정 국면 진입은 부담
  "Michael Burry Locks In Gains on Nvidia, Chipmaker ETF Puts As AI Rout Deepens"
      was '반도체·AI주 반등은 … 부담을 덜어주는 신호'          -> 약세
  "Kalyan-Dombivli Water Crisis: … MIDC Shutdown …, Residents Blame Tanker Lobby"
      was '원유 공급 가동 중단 소식…유가 반응 확인'            -> not an oil story
  "Oil Set for Weekly Gains After US Threatens Indefinite Blockade of Iran" was '원유 공급 차질 우려…'
  "Fed minutes: Higher interest rates needed in December …" was '금리 상승은 미국 주식 부담 신호'
  "USD/JPY Forecast: US CPI, BoJ and Key Levels" was '미국 물가 지표는 금리 부담을 키울 수 있는 신호'
Grouping: an undated release report ("일본 CPI 3.1%…12월 인상 가능성") joins the one dated release; "12월 금리 인상" is
not a release month; six week-ahead calendars are one group of two.
P1-1 ②: a weekend-issued Monday forecast is sent at 08:35 KST, and says so when S&P futures moved 0.5% or more against
the call after their 08:00 reopen.
"""
from datetime import datetime, timezone

import polarmeter_news_rss_probe as probe
import polarmeter_push_outbox as po
from polarmeter_news_fact_semantics import inflation_release


def key(headline: str) -> str:
    return probe.issue_cluster_key({'headline': headline, 'originalHeadline': headline, 'publishedAt': '2026-11-21T01:00:00Z'})


def check_titles() -> None:
    k = probe.koreanize_english_headline
    correction = k('The S&P 500 and Dow Jones indices ended the trading session in correction territory, '
                   'while the Nasdaq wiped out all its year-to-date gains')
    # a plain fact title: the app (1.0.27) rebuilds "…신호" template titles from the English source and read this as a rise
    assert correction == 'S&P500·다우 조정 국면 진입, 나스닥 올해 상승분 반납', correction
    nq = k('Nasdaq 100 Ends Worst Week Since 2020, S&P 500 In Correction')
    assert nq == 'S&P500 조정 국면 진입, 나스닥 2020년 이후 최악의 한 주', nq
    burry = k('Michael Burry Locks In Gains on Nvidia, Chipmaker ETF Puts As AI Rout Deepens')
    assert burry and '약세' in burry and '반등' not in burry, burry
    water = 'Kalyan-Dombivli Water Crisis: 24-Hour MIDC Shutdown Stretches To 36 Hours, Residents Blame Tanker Lobby'
    assert k(water) is None and not probe.is_energy_supply_headline(water), k(water)
    assert probe.is_energy_supply_headline('Russian oil pipeline attack halts exports'), 'a real oil incident stays'
    blockade = k('Oil Set for Weekly Gains After US Threatens Indefinite Blockade of Iran')
    assert blockade == '미국의 이란 봉쇄 위협에 유가 주간 상승 흐름', blockade
    minutes = k('Fed minutes: Higher interest rates needed in December as inflation worries mount')
    assert minutes == '연준 의사록, 12월 금리 인상 필요 의견…금리 부담 신호', minutes
    usdjpy = k('USD/JPY Forecast: US CPI, BoJ and Key Levels')
    assert usdjpy and '전망' in usdjpy and '부담을 키울' not in usdjpy, usdjpy
    assert probe.english_market_context_translation('US CPI data and the dollar') is None, 'a bare mention says nothing'
    convoy = k('Reports Claim Iran Is Preparing Oil Convoy to Test U.S. Blockade')
    assert convoy and convoy.startswith('이란, 미국의 봉쇄 시험') and '봉쇄에 유가' not in convoy, convoy
    assert probe.is_personal_finance_story('"지금 예금 만기인데.." 11월 26일 금리결정, 60대 갱신 전에 봐야 할 조건 1위')
    # unchanged
    assert k('Nasdaq stock market today: 2026 gains erased as Wall Street warns of further losses') == '나스닥 올해 상승분 반납은 미국장 부담 신호'
    # …but such an English card is not listed while app 1.0.27 misreads it (see the selection loop)
    assert probe.ERASED_GAINS_SOURCE.search('Nasdaq stock market today: 2026 gains erased as Wall Street warns of further losses')
    assert probe.ERASED_GAINS_SOURCE.search('the Nasdaq wiped out all its year-to-date gains')
    assert not probe.ERASED_GAINS_SOURCE.search('Nasdaq gains 1% as chip stocks rally')
    assert k('Nasdaq rises as chip stocks rally') == '나스닥 상승 흐름은 미국장 완화 신호'


def check_groups() -> None:
    assert inflation_release('12월 금리 인상 무게…일본 CPI 3.1% 상승')['month'] is None, 'a rate month is not a release month'
    assert inflation_release('일본 10월 CPI 3.1% 상승')['month'] == 10
    heads = ['12월 금리 인상 무게…일본 CPI 3.1% 상승', '일본 CPI 3.1%…12월 인상 가능성', '일본 10월 CPI 3.1% 상승…예상 상회', '일본 CPI 앞두고 엔화 약세']
    joined = probe.join_undated_releases([key(h) for h in heads])
    assert joined[:3] == ['issue:release:inflation:jp:cpi:headline:10'] * 3 and not joined[3].startswith('issue:release:'), joined
    alone = probe.join_undated_releases([key('일본 CPI 3.1%…12월 인상 가능성')])
    assert alone[0].endswith(':nomonth') and probe.issue_limit(alone[0], 3) == 1, alone
    week = ["한은, 금리 인상으로 기울었나...'5천선 붕괴' 증시 향배 등 내주 경제 핫이슈",
            '[다음주 경제] 한은 금리 인상 여부 촉각…성장률 전망·디지털자산법 공개',
            '[11월4주 글로벌 증시 캘린더] 한은 금통위·미국 PCE·GDP…추수감사절 휴장 변수',
            "다음주 코스피, 美 PCE에 '촉각'…추수감사절 거래 위축도 변수",
            'Week Ahead: US GDP, PCE Inflation & Fed Rate Outlook']
    assert {key(h) for h in week} == {'issue:family:week_ahead:all'}, {h: key(h) for h in week}
    assert probe.issue_limit('issue:family:week_ahead:all', 3) == 2
    assert key('이번 주 코스피 7% 급락 마감') != 'issue:family:week_ahead:all', 'a reported close is not a calendar'
    assert key('미국 8월 PCE 3.4% 상승…예상 상회') == 'issue:release:inflation:us:pce:headline:8'
    wrap = {'headline': 'Nasdaq 100 Ends Worst Week Since 2020, S&P 500 In Correction', 'publishedAt': '2026-11-21T01:00:00Z',
            'originalHeadline': 'Nasdaq 100 Ends Worst Week Since 2020, S&P 500 In Correction',
            'displayHeadline': 'S&P500·나스닥 조정 국면 진입은 미국장 부담 신호'}
    assert probe.issue_cluster_key(wrap) == 'issue:us_index_market_wrap', 'a translated close joins the US close group'
    chips = {key('美 반도체 수출규제 전면 확대…삼성, 中 AI칩 출하 중단'), key('젠슨황發 美반도체 수출 규제에 삼성·TSCM도 부담')}
    assert chips == {'issue:family:chip_export_rules'} and probe.issue_limit('issue:family:chip_export_rules', 3) == 1, chips
    assert key('[N위크] 올해 마지막 기준금리 인상?…디지털자산법·금융쇄신안 공개') == 'issue:family:week_ahead:all'


def chart(points: list[tuple[str, float]]) -> dict:
    stamps = [int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()) for t, _ in points]
    return {'chart': {'result': [{'timestamp': stamps, 'indicators': {'quote': [{'close': [c for _, c in points]}]}}]}}


def check_monday_futures() -> None:
    current = {
        'targetSession': '2026-11-23', 'issuedAt': '2026-11-20T21:57:28Z', 'state': 'issued', 'origin': 'live',
        'predicted': {'openCall': 'down'},
        'copy': {'title': {'ko': '월요일 한국장, 크게 밀려서 시작할 것으로 보여요', 'en': 'Monday: Korea looks set to open sharply lower'},
                 'openStat': {'ko': '−2.5% 안팎', 'en': 'around −2.5%'}},
    }
    scorecard = {'live': {'calls': 8, 'openHits': 6}}
    # Friday 16:00 New York = 21:00 UTC; futures reopen Monday 08:00 KST = Sunday 23:00 UTC
    up = chart([('2026-11-20T20:45:00', 100.0), ('2026-11-22T23:00:00', 100.8), ('2026-11-22T23:15:00', 100.7)])
    flat = chart([('2026-11-20T20:45:00', 100.0), ('2026-11-22T23:00:00', 99.6), ('2026-11-22T23:15:00', 99.5)])
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    item = po.morning_item(current, scorecard, at('2026-11-22T23:25:00'), lambda url: up)
    assert item['sendAfter'] == '2026-11-22T23:35:00Z', item['sendAfter']   # 08:35 KST
    assert '미국 선물은 반대로 +0.8%' in item['body']['ko'] and len(item['body']['ko']) <= po.LIMIT_BODY_KO, item['body']
    assert '+0.8%' in item['body']['en'], item['body']
    same = po.morning_item(current, scorecard, at('2026-11-22T23:25:00'), lambda url: flat)
    assert '선물' not in same['body']['ko'] and '토요일 아침 기준' in same['body']['ko'], same['body']
    early = po.morning_item(current, scorecard, at('2026-11-21T03:00:00'), lambda url: up)   # Saturday build
    assert '선물' not in early['body']['ko'] and early['sendAfter'] == '2026-11-22T23:35:00Z', early
    def broken(url):
        raise OSError('no quote')
    assert '선물' not in po.morning_item(current, scorecard, at('2026-11-22T23:25:00'), broken)['body']['ko']
    stale = chart([('2026-11-20T20:45:00', 100.0), ('2026-11-20T21:45:00', 101.0)])   # Friday's last bar only
    assert '선물' not in po.morning_item(current, scorecard, at('2026-11-22T23:25:00'), lambda url: stale)['body']['ko']
    weekday = {**current, 'targetSession': '2026-11-24', 'issuedAt': '2026-11-23T21:50:00Z'}
    assert po.morning_item(weekday, scorecard, at('2026-11-23T22:00:00'), lambda url: up)['sendAfter'] == '2026-11-23T23:05:00Z'


def main() -> None:
    check_titles()
    check_groups()
    check_monday_futures()
    print('PASS news P1 2026-11-22: correction/rout/water/blockade/minutes titles, undated releases and week-ahead groups, Monday futures push')


if __name__ == '__main__':
    main()
