#!/usr/bin/env python3
"""Contract for the 2026-11-21 news / forecast quality check (out/qa-20261121, 형 "퀄리티 보고 수정할 내용있으면").

N1 a year-to-date giveback with a crash is a fall, not an intraday giveback.
N3 an outlook / price-target story is not a rise.
N4 "하락도 뒤집은 … 복귀" is the rebound.
N5/N11 an open or futures report about a session that already closed does not represent that session.
C1 the coach never says stocks were "bought" (reads as the app buying).
"""
from datetime import datetime, timezone

import polarmeter_coach_brief as cb
import polarmeter_news_rss_probe as probe


def check_directions() -> None:
    assert probe.market_burden_tone('AI 거품론과 투매에 무너진 월가… 나스닥 5.28% 폭락하며 올해 상승분 전액 반납') == 'negative'
    assert probe.index_giveback_tone('AI주 투매에 나스닥 5%대 급락…올해 상승분 반납') is None
    assert probe.market_burden_tone('AI 공포에 나스닥 올해 상승분 반납…뉴욕증시 최악의 한 주') == 'negative', 'year-to-date wipe-out is a fall'
    assert probe.index_giveback_tone('코스피 장 초반 상승폭 반납…하락 전환') == 'negative', 'the intraday giveback rule still works'
    assert probe.market_burden_tone('뉴욕증시 하락도 뒤집은 엔비디아…코스피, 하루 만에 5000선 복귀') == 'positive'
    shown = probe.english_market_context_translation('Dow Jones: Wall Street Eyes Rebound as Morgan Stanley Targets S&P 500 at 8,700')
    assert shown and '전망' in shown and '완화 신호' not in shown, shown
    fear = probe.english_market_context_translation('VIX Stock: Wall Street Fear Gauge Surges Above 31 As AI Trade Unravels Across S&P 500, Nasdaq, And Dow Jones')
    assert fear == 'VIX 급등은 미국장 불안이 커진 신호', fear


def at(text: str) -> str:
    return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def check_stale_wrap() -> None:
    kr, us = 'issue:kr_market_flow', 'issue:us_index_market_wrap'
    sat = datetime.fromisoformat('2026-11-21T11:30:00+09:00')
    fri_night = datetime.fromisoformat('2026-11-20T23:50:00+09:00')
    mon = datetime.fromisoformat('2026-11-23T09:30:00+09:00')
    saturday_open = {'headline': '코스피, 20일 장 초반 3.58% 하락', 'publishedAt': at('2026-11-21T08:10:00+09:00')}
    friday_close = {'headline': '코스피 5.21% 급락 마감', 'publishedAt': at('2026-11-20T15:45:00+09:00')}
    monday_open = {'headline': '코스피 장 초반 2% 하락 출발', 'publishedAt': at('2026-11-23T09:10:00+09:00')}
    assert probe.stale_wrap_intraday(saturday_open, kr, sat)
    assert not probe.stale_wrap_intraday(friday_close, kr, sat), 'a close report is never stale'
    assert not probe.stale_wrap_intraday(monday_open, kr, mon), 'the new session open is the latest word'
    friday_morning_open = {'headline': '코스피 장 초반 3% 하락 출발', 'publishedAt': at('2026-11-20T09:10:00+09:00')}
    assert probe.stale_wrap_intraday(friday_morning_open, kr, sat), 'the open of a session that closed'
    us_open_after_close = {'headline': 'Stock Market Today: Dow Rises 0.39% While Nasdaq Drops 1.39% at Open',
                           'publishedAt': at('2026-11-20T23:45:00+09:00')}
    assert probe.stale_wrap_intraday(us_open_after_close, us, sat), 'Friday night open, read on Saturday'
    assert not probe.stale_wrap_intraday(us_open_after_close, us, fri_night), 'the same report while that session is open'
    cpi = {'headline': '[속보] 미국 CPI 예상 상회, 국채 금리 급등·나스닥 선물 하락', 'publishedAt': at('2026-11-21T10:00:00+09:00')}
    assert not probe.stale_wrap_intraday(cpi, us, sat), 'a release that moved futures is news in its own right'


def check_coach_words() -> None:
    for word in ('샀어요', '팔았어요'):
        assert word in cb.KO_ACTION, word
    assert '샀' not in cb.DIRECTION_WORDS


def main() -> None:
    check_directions()
    check_stale_wrap()
    check_coach_words()
    print('PASS news QA 2026-11-21: year-to-date giveback, outlook stories, overcame-the-fall rebound, stale open reports, coach trade words')


if __name__ == '__main__':
    main()
