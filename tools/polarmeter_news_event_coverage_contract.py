"""Offline regression: event collection is separate from direction certainty."""
from polarmeter_news_rss_probe import critical_market_event, DEFAULT_FEEDS, energy_supply_risk, energy_supply_state, koreanize_english_headline

def main():
    for headline in ('원유 송유관 피격', '정유시설 공급 차질', 'Crude pipeline offline after attack', '해협 봉쇄로 항로 중단'):
        assert critical_market_event(headline) == (True, 'energy_transport_disruption'), headline
    for headline in ('송유관 신설 사업 설명', 'Pipeline maintenance training course'):
        assert not critical_market_event(headline)[0], headline
    for suffix in ('kr', 'us'):
        feed = next(f for f in DEFAULT_FEEDS if f['sourceId'] == f'rss:google-news:energy-infrastructure-{suffix}')
        assert feed['url'].startswith('https://news.google.com/rss/search?')
    assert energy_supply_risk('Oil prices surge as Saudi pipeline shutdown continues')
    assert not energy_supply_risk('Pipeline restored after attack')
    assert '사우디 송유관 가동 중단' in koreanize_english_headline('Oil prices surge as Saudi pipeline shutdown continues')
    for title, state in [('원유 공급 차질 우려 완화', 'recovery'), ('원유 공급 차질 재개', 'active'), ('송유관 공격 부인에도 공급 차질 지속', 'mixed'), ('Oil companies struck a deal', 'unknown')]:
        assert energy_supply_state(title) == state, title
    print('PASS energy event coverage: 15 checks (no network)')

if __name__ == '__main__':
    main()
