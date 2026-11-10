#!/usr/bin/env python3
"""Contract (plan S5): implausible-headline filter, topic keys, one card per tone for a market close,
topic/source caps, and the public news summary.

Run: PYTHONPATH=tools python3 tools/polarmeter_news_selection_caps_contract.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import polarmeter_free_cache_worker as worker
import polarmeter_news_rss_probe as probe

ref = {'kospi': 5018.9, 'kosdaq': 1100.0}

# 1. Market-flow reports pass; trading advice and implausible levels do not.
assert not probe.looks_suspicious_headline('코스피 외국인 1조 순매수에 2% 상승', ref)
assert not probe.looks_suspicious_headline('외국인 매도세에 코스닥 약세', ref)
assert not probe.looks_suspicious_headline('코스피 외국인 매도 폭탄…5,000선 붕괴', ref)
assert probe.looks_suspicious_headline('지금 매수할 종목 5선', ref)
assert probe.looks_suspicious_headline('코스피 8,100선 돌파 마감', ref), '+61% of the previous level'
assert not probe.looks_suspicious_headline('코스피 5,120선 회복 마감', ref)
assert probe.looks_suspicious_headline('코스닥 3,000 시대 열렸다', ref)
assert not probe.looks_suspicious_headline('코스피 8,100선 돌파 마감', {}), 'no reference -> no number check'
assert probe.looks_suspicious_headline('코스피 6% 급등', {}), 'a 5%+ move without a backing public move is held back'
assert not probe.looks_suspicious_headline('[속보] 코스피·코스닥 5%대 급등...매수 사이드카 발동', {**ref, 'kospi_pct': 4.8}), \
    'a real 5% day (11-10) is backed by the previous public move'
assert not probe.looks_suspicious_headline('[마감시황] 외국인 6586억 매도 코스피 사흘째 하락', ref)
assert not probe.looks_suspicious_headline('국제유가 급락·AI 저가매수에 나스닥 2.74% 급등 [뉴욕증시]', ref)
assert not probe.looks_suspicious_headline('코스피 반도체 매수에 5300선 돌파 [개장시황]', {'kospi': 5263.5})
assert probe.looks_suspicious_headline('매도 타이밍 놓치지 마세요', ref)
assert probe.looks_suspicious_headline('추격매수 주의보', ref)
assert probe.looks_suspicious_headline('연말 랠리 수혜 5선, 지금 매수해야', ref)
assert not probe.looks_suspicious_headline('코스피, 장 초반 5% 급등해 5200선 탈환…양대 시장 매수 사이드카 발동', {'kospi': 5263.5, 'kospi_pct': 4.7})
assert not probe.looks_suspicious_headline('美 반도체 훈풍에도 코스피 5000선 턱걸이... 外人 매도에 발목', {'kospi': 5263.5})
assert probe.index_reference_levels('kospi=5018.90,kosdaq=1100.00,bad=x') == ref
assert probe.index_reference_levels('') == {}

# The worker passes the previous public levels to the probe.
assert worker.news_index_reference({'signals': {'kospi': {'value': 5018.9, 'changePct': 4.8}, 'kosdaq': {'value': 1100}}}) == \
    'kospi=5018.90,kospi_pct=4.80,kosdaq=1100.00'
assert worker.news_index_reference({}) == ''

# Filtered headlines are counted, not dropped silently.
now = probe.utc_now()
_, summary = probe.normalize_items([{'label': 'QA', 'items': [
    {'headline': '지금 매수할 종목 5선', 'sourceName': 'QA', 'publishedAt': now, 'url': 'https://example.invalid/s1'}]}], 5)
assert summary['filteredReasons'].get('SUSPICIOUS_HEADLINE') == 1

# 2. Bracket tags never split a topic; US close wording variants share one key.
assert probe.strip_headline_tags('[속보]유가 급락에 美증시 2%대 반등').strip() == '유가 급락에 美증시 2%대 반등'
keys = {probe.issue_cluster_key({'headline': headline, 'publishedAt': now}) for headline in (
    '[속보]유가 급락에 美증시 2%대 반등…반도체↑ 엔비디아↓',
    '[글로벌 마켓시황] 유가 급락에 3대 지수 반등…엔비디아 악재',
    '뉴욕증시, 3대 지수 급등…유가 하락에 투자심리 회복',
    '트럼프가 살린 증시…엔비디아 급락[뉴욕마감]',
)}
assert keys == {'issue:us_index_market_wrap'}, keys

# 3. One card per tone for a market close, each the latest of its tone.
group = [{'headline': f'뉴욕증시 3대 지수 상승 {i}', 'impactTone': tone, 'publishedAt': f'2026-11-09T2{i}:00:00Z',
          'url': f'https://example.invalid/w{i}'} for i, tone in enumerate(['positive', 'positive', 'negative', 'positive'])]
kept = probe.issue_capped_items(group, per_issue_limit=3)
assert sorted((item['impactTone'], item['publishedAt']) for item in kept) == [
    ('negative', '2026-11-09T22:00:00Z'), ('positive', '2026-11-09T23:00:00Z')], kept

# 4. Caps: a crowded day stays at 30 cards, and diverse candidates are preferred over a crowded source.
crowded = [{'headline': f'사우디 원유 송유관 {i}곳 피격으로 공급 차질, 유가 급등', 'sourceName': 'Same',
            'publishedAt': now, 'url': f'https://example.invalid/c{i}'} for i in range(40)]
diverse = [{'headline': f'코스피 {i}% 상승 마감, 외국인 수급 개선', 'sourceName': f'Src{i}', 'publishedAt': now,
            'url': f'https://example.invalid/d{i}'} for i in range(1, 3)]
selected, summary = probe.normalize_items([{'label': 'QA', 'items': crowded + diverse}], 30)
assert len(selected) == 30
assert summary['filteredReasons'].get('CAPPED_REFILL', 0) > 0, 'over-cap refill is counted'
assert probe.MAX_CARDS_PER_SOURCE == 3 and probe.MAX_CARDS_PER_CATEGORY == 8

# 5. Public news summary.
with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
    report = Path(folder) / 'news.json'
    report.write_text(json.dumps({'filteredReasons': {'SUSPICIOUS_HEADLINE': 2, 'OTHER': 9},
                                  'feedResults': [{'status': 'ok'}, {'status': 'error'}]}), encoding='utf-8')
    snapshot = {'news': {'items': [
        {'impactTone': 'positive', 'sourceName': 'A', 'sourceId': 'rss:google-news:x'},
        {'impactTone': 'negative', 'sourceName': 'B', 'sourceId': 'rss:cnbc', 'critical': True}]}}
    quality = worker.news_quality_summary(snapshot, report)
assert quality == {'cards': 2, 'tones': {'positive': 1, 'neutral': 0, 'negative': 1}, 'sources': 2, 'critical': 1,
                   'directRss': 1, 'filtered': {'SUSPICIOUS_HEADLINE': 2}, 'feeds': {'ok': 1, 'total': 2}}, quality
assert 'news' in worker.sanitize_public_data_quality({'news': quality})

print('polarmeter news selection caps contract passed')
