#!/usr/bin/env python3
"""2026-11-24: Jensen Huang's return (Nvidia +30%, Nasdaq +8%) reached English wires at 23:11 KST, but every
English headline was dropped as untranslated and the first card landed at 00:08. English big-move headlines from
trusted wires now carry over fact for fact (who, how much, why), never a cumulative move, an offer or a weight."""
from __future__ import annotations

import polarmeter_news_rss_probe as probe

TRANSLATE = {
    'Nvidia Stock Rises 28% After Jensen Huang Reinstated as CEO': '젠슨 황 CEO 복귀에 엔비디아 주가 28% 급등',
    "Nvidia’s stock soars 30%-plus as Jensen Huang returns as CEO": '젠슨 황 CEO 복귀에 엔비디아 주가 30% 넘게 급등',
    'Nasdaq Futures Halted After 7% Surge as Nvidia Brings Back Jensen Huang': '젠슨 황 CEO 복귀에 나스닥 선물 7% 급등·거래 일시 중단',
    'S&P 500 surges 5% after Nvidia reinstates Jensen Huang as CEO': '젠슨 황 CEO 복귀에 S&P500 5% 급등',
    "Stock Market Today: Dow Surges 1,200 Points As Nvidia Soars 30% On Jensen Huang's Return (Live Coverage)":
        '젠슨 황 CEO 복귀에 다우 1,200포인트 급등…엔비디아 주가 30% 급등',
    'Stocks Surge After Jensen Huang Is Reinstated as Nvidia CEO': '젠슨 황 CEO 복귀에 미국 증시 급등',
    'AI-Linked Stocks Take Off After Nvidia Reinstates Jensen Huang': '젠슨 황 CEO 복귀에 AI·반도체주 급등',
    'Nvidia Rebound Sends Dow Futures Up 1100 Points': '다우 선물 1100포인트 상승',
    'US stock futures fall ahead of Nvidia earnings: 5 things to know': '실적 발표 앞두고 미국 주가지수 선물 하락',
}
NO_BIG_MOVE = [
    'Can Jensen Huang Return to Nvidia?',
    'Jensen Huang Offered to Come Back and Nvidia Gained 6%',
    'Nvidia Is Only 17% of SMH. After Its Beat, the Chip ETF Rallied',
    'Nvidia Has Fallen 18% Since the Jensen Huang Scandal',
    'Dow Jones falls 1,600 points in two sessions, but analysts see a bottom',
    'Nasdaq-100 Ends 49 Points From a Bear Market; Nvidia Off 47%',
]
CRITICAL = [
    'Nvidia Stock Rises 28% After Jensen Huang Reinstated as CEO',
    '젠슨 황 복귀에 엔비디아 28% 폭등…나스닥 5.9% 급등',
    "Stock Market Today: Dow Surges 1,200 Points As Nvidia Soars 30% On Jensen Huang's Return",
]
NOT_CRITICAL = [
    'Dow Jones falls 1,600 points in two sessions, but analysts see a bottom',
    'Nvidia Has Fallen 18% Since the Jensen Huang Scandal',
    '엔비디아, 이번 주 누적 18% 하락',
    '애플 주가 2% 하락',
]


def main() -> int:
    failures = []
    for source, want in TRANSLATE.items():
        got = probe.koreanize_english_headline(source)
        if got != want:
            failures.append(f'translate {source!r}: {got!r} != {want!r}')
    for source in NO_BIG_MOVE:
        if probe.english_big_move_fact_headline(source):
            failures.append(f'no big move expected: {source!r} -> {probe.english_big_move_fact_headline(source)!r}')
    for source in CRITICAL:
        if not probe.critical_market_event(source)[0]:
            failures.append(f'critical expected: {source!r}')
    for source in NOT_CRITICAL:
        if probe.critical_market_event(source)[0]:
            failures.append(f'not critical expected: {source!r}')
    # Content farms do not get the fact-for-fact translation.
    feed = [{'sourceId': 'rss:test', 'label': 'Google News RSS — test', 'region': 'US', 'status': 'ok', 'items': [
        {'headline': 'Nvidia Stock Rises 28% After Jensen Huang Reinstated as CEO', 'sourceName': 'Udaipur Kiran',
         'publishedAt': probe.utc_now(), 'url': 'https://news.google.com/rss/articles/farm?oc=5'},
        {'headline': 'Nvidia’s stock soars 30%-plus as Jensen Huang returns as CEO', 'sourceName': 'MarketWatch',
         'publishedAt': probe.utc_now(), 'url': 'https://www.marketwatch.com/story/nvidia-huang-returns'},
    ]}]
    items, meta = probe.normalize_items(feed, 30)
    heads = [item.get('displayHeadline') for item in items]
    if '젠슨 황 CEO 복귀에 엔비디아 주가 30% 넘게 급등' not in heads:
        failures.append(f'trusted wire missing: {heads}')
    if any('28%' in (head or '') for head in heads) or not meta['filteredReasons'].get('BIG_MOVE_UNTRUSTED_SOURCE'):
        failures.append(f'content farm kept: {heads} {meta["filteredReasons"]}')
    if failures:
        print('\n'.join('FAIL ' + line for line in failures))
        return 1
    print('PASS news big move 2026-11-24: trusted English wires translated fact for fact, critical heavyweight moves')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
