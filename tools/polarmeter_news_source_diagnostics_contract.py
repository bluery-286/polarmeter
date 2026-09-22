"""Offline source diagnostics and worker-boundary regression checks."""
import contextlib
import io
import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import patch

import polarmeter_github_pages_prepare as prepare
import polarmeter_news_source_diagnostics as diagnostics
import polarmeter_news_rss_probe as probe


def main():
    company_probe = "43억 달러 합의 3년 만에...바이낸스, '이란 제재'로 美 검찰 조사"
    systemic_probe = '바이낸스 검찰 조사 여파 금융시장 유동성 위기, 나스닥 급락'
    assert probe.isolated_company_enforcement(company_probe)
    assert not probe.isolated_company_enforcement(systemic_probe)
    assert not probe.isolated_company_enforcement('미국, 중국 반도체 기업 수출 제재 확대')
    assert not probe.isolated_company_enforcement('중앙은행 금융시장 유동성 조사 발표')
    assert probe.critical_market_event(company_probe)[0] is False
    matched = [rule for rule in probe.MARKET_RELEVANCE_RULES if probe.rule_matches_headline(rule, company_probe, company_probe.lower())]
    score = probe.market_impact_components(company_probe, 'fixture', matched, 1)
    assert score['isolatedCompanyEnforcement'] is True and score['breadth'] <= 25
    assert score['marketImpactScore'] < probe.MARKET_IMPACT_THRESHOLD
    assert probe.classify_relevance(company_probe, 'fixture', probe.utc_now())[0] is None
    assert probe.classify_relevance(systemic_probe, 'fixture', probe.utc_now())[0] is not None
    givebacks = [
        ('코스피, 2.2% 급등 출발 뒤 상승분 반납…7000선 턱걸이', 'neutral'),
        ('코스피 상승분 반납 뒤 1% 하락 마감', 'negative'),
        ('코스피 상승분 반납 뒤 다시 반등, 1% 상승 마감', 'positive'),
        ('코스피 상승분 반납 뒤 하락 전환했지만, 다시 1% 상승 마감', 'positive'),
        ('코스피 상승분 반납, 하락 마감 우려', 'neutral'),
    ]
    for headline, expected in givebacks:
        assert probe.index_giveback_tone(headline) == expected
        assert probe.market_burden_tone(headline, 'positive') == expected
        result, _ = probe.normalize_items([{'sourceId': 'rss:google-news:market-context-kr', 'items': [
            {'headline': headline, 'url': 'https://example.com/giveback', 'sourceName': 'fixture', 'region': 'KR', 'publishedAt': probe.utc_now()}
        ]}], max_items=30)
        assert result and result[0]['impactTone'] == expected
        assert '반납' in result[0]['displayHeadline']
    assert probe.index_giveback_tone('코스피, 2.2% 상승 마감') is None
    assert probe.index_giveback_tone('코스피 상승분 반납 없이 강세 마감') is None
    easing = 'US Stock Market Today: Dow Gains 300 Points, S&P 500 And Nasdaq Rise As Oil, Treasury Yields Ease'
    easing_display = probe.koreanize_english_headline(easing)
    assert '유가·국채 금리 하락' in easing_display and '지수 상승' in easing_display
    assert '유가도 올라' not in easing_display
    assert '지수 상승' in probe.koreanize_english_headline(easing.replace('Ease', 'Fall'))
    assert not probe.directly_observed_index_rise('Dow gains but Nasdaq falls as oil, Treasury yields ease')
    assert not probe.coordinated_oil_yield_easing('Olive oil yields fall after drought')
    for original, entity in [
        ('Drones attack oil refinery in Samara Oblast of russia', '사마라'),
        ('Drones attack Kuibyshev oil refinery in Russia', '쿠이비셰프'),
        ('The East-West Pipeline shutdown comes as oil markets are on edge', '동서'),
        ('Sharara Oilfield Faces Force Majeure Risk after Pipeline Shutdown', '샤라라'),
    ]:
        assert entity in probe.koreanize_english_headline(original), 'energy event identity lost'
    export_response = 'Saudi Arabia Increases Gulf Oil Exports Following East-West Pipeline Attack'
    assert '수출 확대' in probe.koreanize_english_headline(export_response), 'latest export response lost behind historical attack'
    assert '공급 공급' not in probe.koreanize_english_headline('Oil Jumps 7% to Above $100 Ahead of US Blockade on Strait of Hormuz')
    integrated, _ = probe.normalize_items([{'sourceId': 'rss:google-news:macro-releases-us', 'items': [
        {'headline': headline, 'publishedAt': probe.utc_now(), 'url': f'https://example.com/integrated/{index}', 'sourceName': 'fixture', 'region': 'US'}
        for index, headline in enumerate([easing, export_response])
    ]}], max_items=30)
    assert len(integrated) == 2
    integrated_by_url = {item['url']: item for item in integrated}
    ease_item = integrated_by_url['https://example.com/integrated/0']
    assert ease_item['impactTone'] == 'positive' and '국채 금리는 하락' in ease_item['whyImportant'], 'full pipeline re-inverted falling costs'
    assert integrated_by_url['https://example.com/integrated/1']['impactTone'] == 'neutral'
    resume = 'Port of Fujairah Resumes Oil Loadings After Attack and Why it Matters Globally'
    completed = 'Atmos Energy outage repairs complete in Little Elm after contractor damages natural gas pipeline'
    assert probe.energy_supply_state(resume) == 'recovery'
    assert probe.energy_supply_state('Port plans to resume oil loadings after attack') != 'recovery'
    assert '재개' in probe.koreanize_english_headline(resume)
    assert probe.energy_supply_state(completed) == 'recovery'
    assert probe.classify_relevance(completed, 'fixture', probe.utc_now())[1] == 'LOCAL_UTILITY_NOT_MARKET_TEMPERATURE'
    assert '하락' in probe.tone_aligned_why('Wall Street rallies as AI optimism reignites, Treasury yields retreat', '', '금리가 높아지면 부담', 'positive')
    feed = {'sourceId': 'test-feed', 'url': 'https://example.com/rss', 'label': 'test', 'region': 'US'}
    response = io.BytesIO(b'<rss><channel><item><title>Market report</title><link>https://example.com/article</link></item></channel></rss>')
    with patch.object(probe.urllib.request, 'urlopen', side_effect=[TimeoutError(), response]) as request, patch.object(probe.time, 'sleep'):
        recovered = probe.fetch_feed(feed, timeout=1, limit=2)
    assert request.call_count == 2 and recovered['status'] == 'ok'
    assert recovered['attemptCount'] == 2 and recovered['firstErrorType'] == 'TimeoutError'
    unordered = io.BytesIO(b'<rss><channel>'
        b'<item><title>Older market report</title><link>https://example.com/older</link><pubDate>Mon, 21 Sep 2026 08:00:00 GMT</pubDate></item>'
        b'<item><title>Breaking CPI release</title><link>https://example.com/newer</link><pubDate>Tue, 22 Sep 2026 08:00:00 GMT</pubDate></item>'
        b'</channel></rss>')
    with patch.object(probe.urllib.request, 'urlopen', return_value=unordered):
        latest = probe.fetch_feed(feed, timeout=1, limit=1)
    assert latest['items'][0]['url'] == 'https://example.com/newer', 'feed order must not hide a newer story beyond the limit'
    with patch.object(probe.urllib.request, 'urlopen', side_effect=TimeoutError()) as request, patch.object(probe.time, 'sleep'):
        failed = probe.fetch_feed(feed, timeout=1, limit=2)
    assert request.call_count == 2 and failed['status'] == 'error'
    for code in (401, 403, 429):
        with patch.object(probe.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError(feed['url'], code, '', {}, None)) as request, patch.object(probe.time, 'sleep'):
            probe.fetch_feed(feed, timeout=1, limit=2)
        assert request.call_count == 1
    with patch.object(probe.urllib.request, 'urlopen', return_value=io.BytesIO(b'<rss><channel/></rss>')) as request:
        empty = probe.fetch_feed(feed, timeout=1, limit=2)
    assert request.call_count == 1 and empty['status'] == 'empty'
    marker = 'UNTRUSTED_REPORT_TEXT_MUST_NOT_APPEAR'
    source = sorted(diagnostics.SOURCE_IDS)[0]
    report = {
        'feedResults': [
            {'sourceId': source, 'status': 'error', 'error': 'TimeoutError', 'items': [], 'url': marker},
            {'sourceId': marker, 'status': marker, 'error': marker, 'items': []},
        ],
        'items': [{}, {}],
        'filteredReasons': {'DUPLICATE': 3, marker: 4},
        'filteredOutCount': 7,
    }
    summary = diagnostics.summarize_news_sources(report)
    assert summary['selectedCount'] == 2
    assert summary['sources'][0]['errorType'] == 'TimeoutError'
    assert summary['filteredReasons'] == {'DUPLICATE': 3}
    assert summary['otherFilteredCount'] == 4
    assert marker not in json.dumps(summary)
    assert diagnostics.summarize_news_sources([]) == {'reportStatus': 'invalid'}
    assert diagnostics.safe_count(True) is None
    assert diagnostics.safe_count(10 ** 10000) is None
    coverage = diagnostics.topic_coverage({
        'feedResults': [{'items': [{'headline': '미국 CPI 물가 발표 예상 상회'}]}],
        'items': [],
    })
    assert coverage['inflation']['reviewSelectionGap'] is True
    assert coverage['employment']['reviewSelectionGap'] is False
    assert marker not in json.dumps(coverage)
    assert any(feed['sourceId'].endswith('macro-releases-kr') for feed in probe.DEFAULT_FEEDS)
    assert any(feed['sourceId'].endswith('macro-releases-us') for feed in probe.DEFAULT_FEEDS)
    assert any(feed['sourceId'].endswith('policy-shocks-kr') for feed in probe.DEFAULT_FEEDS)
    major_headlines = [
        '[속보] 미국 CPI 예상 상회, 국채 금리 급등·나스닥 선물 하락',
        '[속보] 연준 FOMC 기준금리 0.25%p 인하 발표',
        '[속보] 미국 비농업 고용 예상 하회, 실업률 상승',
        '[속보] 미국 대중국 관세 인상 발표, 반도체 수출통제 확대',
        '[속보] 원달러 환율 급등, 코스피 서킷브레이커 발동',
    ]
    fixtures = [{'sourceId': source, 'status': 'ok', 'region': 'KR', 'items': [
        {'headline': headline, 'publishedAt': probe.utc_now(),
         'url': f'https://example.com/fixture/{index}', 'sourceName': 'fixture', 'region': 'KR'}
        for index, headline in enumerate(major_headlines)
    ]}]
    selected, _ = probe.normalize_items(fixtures, max_items=30)
    assert len(selected) == len(major_headlines), 'major market events must survive the complete selection path'

    # A burst of energy stories must not crowd every macro release out.
    crowding = [f'사우디 원유 송유관 {index}곳 피격으로 공급 차질, 유가 급등'
                for index in range(1, 41)] + major_headlines[:3]
    crowded_feed = [{'sourceId': source, 'status': 'ok', 'region': 'US', 'items': [
        {'headline': headline, 'publishedAt': probe.utc_now(),
         'url': f'https://example.com/crowding/{index}', 'sourceName': 'fixture', 'region': 'US'}
        for index, headline in enumerate(crowding)
    ]}]
    crowded_selected, _ = probe.normalize_items(crowded_feed, max_items=30)
    selected_urls = {item['url'] for item in crowded_selected}
    assert len(crowded_selected) == 30
    assert all(f'https://example.com/crowding/{index}' in selected_urls for index in (40, 41, 42)), 'macro releases lost in energy-story burst'

    # Region quotas alone do not guarantee index or distinct macro coverage.
    anchors = major_headlines[:3] + ['코스피 1% 상승 마감', easing]
    burst = [f'사우디 원유 송유관 {index}곳 피격으로 공급 차질, 유가 급등' for index in range(1, 41)] + anchors
    burst_feed = [{'sourceId': source, 'status': 'ok', 'items': [
        {'headline': headline, 'publishedAt': probe.utc_now(), 'url': f'https://example.com/anchors/{index}',
         'sourceName': 'fixture', 'region': 'US' if index < 40 or index == 44 else 'KR'}
        for index, headline in enumerate(burst)
    ]}]
    anchor_selected, _ = probe.normalize_items(burst_feed, max_items=30)
    anchor_urls = {item['url'] for item in anchor_selected}
    assert all(f'https://example.com/anchors/{index}' in anchor_urls for index in range(40, 45)), 'index/macro evidence crowded out by crisis volume'
    # An older high-score cluster must not remove its latest qualifying update.
    cluster = [{'headline': f'코스피 {index}% 상승 마감', 'url': f'https://example.com/cluster/{index}',
                'publishedAt': f'2026-09-22T0{index}:00:00Z'} for index in range(4)]
    capped = probe.issue_capped_items(cluster, per_issue_limit=3)
    assert any(item['url'].endswith('/3') for item in capped), 'latest cluster update was dropped'

    # Both worker success (even 2 items) and failure must leave bounded evidence.
    for exit_code in [0, 1]:
        def child(cmd, **kwargs):
            destination = Path(cmd[cmd.index('--news-report') + 1])
            destination.write_text(json.dumps(report), encoding='utf-8')
            return subprocess.CompletedProcess(cmd, exit_code, stdout='{}', stderr='')
        captured = io.StringIO()
        with patch.object(prepare.subprocess, 'run', side_effect=child), contextlib.redirect_stderr(captured):
            try:
                result = prepare.run_worker(Path('unused-output'), Path('unused-last-good'), attempts=1)
                assert exit_code == 0 and result == {}
            except RuntimeError:
                assert exit_code == 1
        assert 'newsSourceDiagnostics' in captured.getvalue()
        assert 'TimeoutError' in captured.getvalue()
        assert marker not in captured.getvalue()
    captured = io.StringIO()
    with patch.object(Path, 'read_text', side_effect=OSError(marker)), contextlib.redirect_stderr(captured):
        diagnostics.emit_news_source_diagnostics(Path('absent'))
    assert 'unreadable' in captured.getvalue() and marker not in captured.getvalue()
    print('PASS safe news source diagnostics, success/failure worker paths')


if __name__ == '__main__':
    main()
