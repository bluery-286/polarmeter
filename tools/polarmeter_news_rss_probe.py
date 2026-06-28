#!/usr/bin/env python3
"""Fetch public RSS headlines for PolarMeter B1 cached-news QA.

Contract:
- server/worker side only; app clients never call RSS providers directly
- headline/source/published_at/url only
- no article body, image scraping, or paid API
"""
from __future__ import annotations

import argparse
import email.utils
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = WORKSPACE / 'testflight/news-rss-probe-latest.json'
DEFAULT_TIMEOUT = 8
USER_AGENT = 'PolarMeter-B1-NewsCache/0.1 (+https://bluery-286.github.io/polarmeter/support.html)'
MAX_NEWS_AGE_HOURS = 24
DEFAULT_NEWS_TTL_MINUTES = 30
NEWS_RECOMMENDED_SCHEDULE = '30min_weekdays_60min_weekends_public_headline_cache'

CRITICAL_NEWS_KEYWORDS = [
    # 지정학 완화/악화 — 시장 위험선호와 유가·변동성에 직접 연결되는 이벤트
    '이란', '중동', '휴전', '종전', '전쟁 종료', '협상 타결', '평화 협정', '합의 시사',
    '군사 충돌', '공습', '확전', '제재 해제', '전쟁',
    'iran', 'ceasefire', 'truce', 'armistice', 'peace deal', 'iran deal', 'deal coming soon',
    'war risk', 'airstrike', 'escalation', 'sanctions lifted',
    # 중앙은행/매크로 서프라이즈
    '긴급 금리', '금리 인하 시사', '빅스텝', 'cpi surprise', 'fomc emergency', 'emergency rate',
    # 개장 방향을 선행하는 지수 급변
    '나스닥 선물', 's&p futures', 'nasdaq futures', 'dow futures', '갭업', '갭다운',
]

CRITICAL_MARKET_MOVE_PATTERNS = [
    re.compile(r'(나스닥|S&P500|S&P 500|코스피|코스닥)[^\n]{0,24}(2\.\d|[3-9])%[^\n]{0,12}(상승|급등|하락|급락|폭락)', re.I),
    re.compile(r'(nasdaq|s&p\s*500|dow)[^\n]{0,24}(jumps?|surges?|rallies|plunges?|crashes?|drops?)[^\n]{0,24}(2\.\d|[3-9])%', re.I),
]

EXCLUDED_SOURCE_HINTS = [
    '프리미엄콘텐츠',
    'premium content',
    'blog',
]

EXCLUDED_HEADLINE_HINTS = [
    '목표가',
    '추천주',
    '특징주',
    '상한가',
    '리딩방',
    '무료추천',
    '회원전용',
    '공모주 청약',
    'buy ',
    ' buy',
    'sell ',
    ' sell',
    'free cash flow',
    'capital gains',
    'cap gains',
    'tax-free',
    'should you actually buy',
    'best semiconductor etf to buy',
    'better s&p 500 etf',
    'voo vs. spy',
    'better stock',
    'dividend stock',
    'income investors',
    'stock buys',
    'one fund that compounds',
    'median earner needs',
    'outperforming the broader market',
    'these stocks instead',
    'roth ira',
    'social security',
    'tax breaks',
    'annuity',
    'retirement',
    'retiree',
    'retirees',
    'bond ladder',
    'cash and bond ladder',
    'your portfolio',
    'for your portfolio',
    'lock in yields',
    'lock in 5%',
    'yield investors',
    'yield strategy',
    'income etf',
    '커버드콜 etf',
    '월분배',
    '월 분배',
    'etf 상장',
    'fang income etf',
    'robinhood traders',
    'piling into',
    'bull run',
    'too good to be true',
    'dividend stocks',
    '주요공시',
    'ipo',
    'm&a',
    '집값',
    '부동산',
    '아파트',
    'gtx',
]


MARKET_IMPACT_THRESHOLD = 45.0

MARKET_LINKAGE_PATTERNS = [
    re.compile(r'(코스피|코스닥|나스닥|S&P\s*500|S&P500|SP500|다우|VIX)', re.I),
    re.compile(r'(환율|원/달러|원달러|달러|금리|연준|FOMC|10년물|Treasury|yield)', re.I),
    re.compile(r'(유가|WTI|원유|중동|이란|전쟁|휴전|제재|관세|수출규제|공급망)', re.I),
    re.compile(r'(외국인\s*(투자자|기관)|기관\s*(순매수|순매도)|순매수|순매도|급락|급등|폭락|반등|하락|상승|서킷브레이커|사이드카)', re.I),
]

FOREIGN_FLOW_MARKET_PATTERN = re.compile(
    r'((외국인|기관)\s*(투자자|순매수|순매도|매수세|매도세|수급|자금)'
    r'|(외국인|기관).{0,8}(순매수|순매도|매수세|매도세|수급))',
    re.I,
)

MAGNITUDE_PATTERNS = [
    re.compile(r'\d+(?:\.\d+)?\s*(%|bp|bps|조|억|달러|원|원/달러|p|포인트)', re.I),
    re.compile(r'(급등|급락|폭등|폭락|돌파|최대|최저|사상|역대|긴급|충격|랠리|selloff|plunge|surge|record)', re.I),
]

SOURCE_QUALITY_PENALTY_PATTERNS = [
    re.compile(r'(팝업스토어|팝업\s*스토어|가봤더니|팬덤|브랜드\s*캠페인|맛집|신제품|편의점|성수|외국인\s*잡아|협찬|광고|프로모션)', re.I),
]

INVESTMENT_ADVICE_PATTERNS = [
    re.compile(r'(목표가|추천주|상한가|공모주|청약|매수|매도|should\s+you\s+buy|which\s+stock)', re.I),
]

CIVIC_LIFESTYLE_POLICY_PATTERNS = [
    re.compile(r'(관광객|관광\s*지원|지역\s*관광|여행\s*지원|축제\s*보조|지역\s*축제|전통시장\s*행사)', re.I),
    re.compile(r'(버스\s*요금|버스요금|지하철\s*요금|지하철요금|택시비|교통비\s*지원|요금\s*할인\s*지원)', re.I),
    re.compile(r'(지역화폐|생활비\s*지원|명절\s*지원|관리비\s*지원|주거\s*바우처|청년\s*임대)', re.I),
    re.compile(r'(가스비\s*할인|전기료\s*지원|전기요금\s*지원|난방비\s*지원)', re.I),
]

LOCAL_WELFARE_DONATION_PATTERNS = [
    re.compile(r'(이웃\s*돕기|불우\s*이웃|취약\s*계층|저소득층|독거\s*노인|사랑의\s*열매|적십자)', re.I),
    re.compile(r'(성금|후원금|후원\s*물품|물품\s*기탁|쌀\s*기탁|연탄|김장|장학금|자원봉사|나눔\s*행사)', re.I),
    re.compile(r'(복지관|복지\s*센터|외국인\s*센터|주민\s*센터|행정복지\s*센터|군청|구청|새마을)', re.I),
    re.compile(r'([가-힣]+읍|[가-힣]+면|[가-힣]+군).{0,20}(기탁|성금|후원|나눔|봉사)', re.I),
    re.compile(r'(기탁|성금|후원|나눔|봉사).{0,20}([가-힣]+읍|[가-힣]+면|[가-힣]+군)', re.I),
]

CIVIC_POLICY_MARKET_OVERRIDE_PATTERNS = [
    re.compile(r'(코스피|코스닥|나스닥|S&P\s*500|S&P500|SP500|VIX|증시|주가|상장|실적|매출|영업이익|가이던스|컨센서스)', re.I),
    re.compile(r'(소비\s*지수|소매\s*판매|내수|CPI|물가|인플레이션|유통업|숙박업|항공주|여행주|면세점|수혜\s*섹터)', re.I),
    re.compile(r'(산업용|기업|생산|제조업|수출|무역|관세|세제|법인세|자본이득세|공급망|물류|항만|에너지\s*가격)', re.I),
    re.compile(r'(외국인\s*(투자자|기관)|순매수|순매도|기관\s*(순매수|순매도))', re.I),
]

RETAIL_FUEL_PRICE_PATTERNS = [
    re.compile(r'(주유소|기름값|휘발유|경유|유류세|유류비|운전자)', re.I),
]

RETAIL_FUEL_MARKET_OVERRIDE_PATTERNS = [
    re.compile(r'(WTI|브렌트|Brent|원유\s*선물|정유주|에너지주|OPEC|호르무즈|중동|이란|CPI|물가|인플레이션)', re.I),
]

SINGLE_COMPANY_LISTING_PATTERNS = [
    re.compile(r'(이전\s*상장|이전상장|상장\s*예비\s*심사|상장예비심사|상장\s*예심|코스피\s*이전)', re.I),
    re.compile(r'(나스닥|뉴욕|미국).{0,12}(상장|adr|주식예탁증서)|(?:adr|주식예탁증서).{0,16}(발행|상장)', re.I),
    re.compile(r'(나스닥|뉴욕|미국).{0,8}(택한|택했다|선택한).{0,24}(하이닉스|삼성|기업|회사)|(?:하이닉스|삼성|기업|회사).{0,24}(나스닥|뉴욕|미국).{0,8}(택한|택했다|선택한)', re.I),
    re.compile(r'코스닥.{0,16}(데뷔|입성)|(?:데뷔|입성).{0,16}코스닥', re.I),
]

LISTING_MARKET_OVERRIDE_PATTERNS = [
    re.compile(r'(코스닥\s*지수|코스닥시장\s*전체|지수\s*편출입|수급\s*충격|외국인\s*수급|기관\s*수급)', re.I),
]

SCIENCE_TECH_NONMARKET_PATTERNS = [
    re.compile(r'(DNA|유전자|연구진|논문|실험|기술\s*개발|개발했다|제조\s*혁신|바이오\s*제조|물에서)', re.I),
]

SCIENCE_TECH_MARKET_OVERRIDE_PATTERNS = [
    re.compile(r'(실적|매출|영업이익|수출|가이던스|컨센서스|주가|증시|ETF|SOXX|SMH|엔비디아|NVIDIA|삼성전자|하이닉스|마이크론)', re.I),
]

LOW_IMPACT_POLICY_NOISE_PATTERNS = [
    re.compile(r'(산지\s*전용|인허가|허가\s*완화)', re.I),
]

LOW_IMPACT_POLICY_MARKET_OVERRIDE_PATTERNS = [
    re.compile(r'(주가|증시|수급|실적|매출|영업이익|수출|관세|공급망|대규모\s*투자|투자\s*계획)', re.I),
]

LOCAL_SEMICONDUCTOR_POLICY_NOISE_PATTERNS = [
    re.compile(r'(노조|노노\s*갈등|탈퇴|임단협|쟁의|노사\s*갈등).{0,40}(삼성|하이닉스|반도체)|(삼성|하이닉스|반도체).{0,40}(노조|노노\s*갈등|탈퇴|임단협|쟁의|노사\s*갈등)', re.I),
    re.compile(r'(호남|광주|전남|지역갈등|여의도\s*정치|정면충돌|특혜|직권남용|이천시|산단|소부장\s*거점|고졸\s*인재|고교|특성화고|마이스터고|충북반도체고|학생|예산\s*축소).{0,48}(반도체|소부장)|(반도체|소부장).{0,48}(호남|광주|전남|지역갈등|여의도\s*정치|정면충돌|특혜|직권남용|이천시|산단|소부장\s*거점|고졸\s*인재|고교|특성화고|마이스터고|충북반도체고|학생|예산\s*축소)', re.I),
]

LOCAL_SEMICONDUCTOR_POLICY_MARKET_OVERRIDE_PATTERNS = [
    re.compile(r'(주가|증시|코스피|코스닥|지수|수급|외국인|기관|실적|매출|영업이익|수출|업황|가이던스|컨센서스|생산\s*차질|공급\s*차질|가동\s*중단)', re.I),
]

THEME_OR_OPINION_NOISE_PATTERNS = [
    re.compile(r'(더\s*오른다|매력도\s*높아지는|시점\s*올\s*것|코스닥의\s*봄|내\s*계좌|개미들|감감무소식|ETF는\s*감감무소식|전쟁용\s*반도체\s*카드|승자의\s*저주|패권\s*전쟁\s*시대|두\s*번째\s*심장|대한민국\s*두\s*번째\s*심장|호남이|돼야\s*한다|코스닥\s*30주년|도전의\s*역사|삼전닉스|탕후루)', re.I),
    re.compile(r'코스피.{0,28}(1\s*만|10,000|10000|[89]\s*천|[89],000)|(?:1\s*만|10,000|10000|[89]\s*천|[89],000).{0,28}코스피', re.I),
    re.compile(r'(Prediction:|Could\s+Crush|Should\s+You\s+Actually|Smarter\s+Buy|Best\s+.+\s+To\s+Buy)', re.I),
]

CORPORATE_CRIME_NONMARKET_PATTERNS = [
    re.compile(r'(기술|영업\s*비밀|자료|정보).{0,24}(유출|넘긴|빼돌|절취|실형|징역|구속|재판|기소)', re.I),
    re.compile(r'(실형|징역|구속|재판|기소|횡령|배임).{0,24}(반도체|기술|임원|연구원|직원)', re.I),
]

CORPORATE_CRIME_MARKET_OVERRIDE_PATTERNS = [
    re.compile(r'(주가|증시|지수|실적|매출|영업이익|수출|공급망|가이던스|컨센서스|거래\s*정지)', re.I),
]

POLITICAL_CONTEXT_NONMARKET_PATTERNS = [
    re.compile(r'(midterm|midterms|election|elections|대선|총선|중간\s*선거|선거|정치권|politics)', re.I),
]

POLITICAL_CONTEXT_MARKET_OVERRIDE_PATTERNS = [
    re.compile(r'(stocks?|market|s&p\s*500|sp500|nasdaq|dow|futures?|yield|treasury|rate\s*decision|rate\s*cut|rate\s*hike|증시|시장|지수|선물|국채|수익률|금리\s*(인하|인상|동결|결정))', re.I),
]


def is_outlook_commentary(text: str) -> bool:
    market = re.search(r'(코스피|코스닥|나스닥|s&p|sp500|다우|증시|지수|시장|wall street|stocks?)', text, re.I)
    outlook = re.search(r'(전망|분석|예상|내다봤|가능|목표치|could|prediction|forecast|target|targets|강세장이면|숨고르기)', text, re.I)
    future_or_conditional = re.search(r'(향후|내년|올해|까지|10년|2026|강세장이면|가능|could|prediction|forecast|target|targets|숨고르기)', text, re.I)
    fresh_market_wrap = re.search(r'(마감|개장|선물|프리뷰|급등\s*마감|상승\s*마감|하락\s*마감|약세\s*출발|강세\s*출발)', text, re.I)
    return bool(market and outlook and future_or_conditional and not fresh_market_wrap)


def is_civic_lifestyle_policy(text: str) -> bool:
    return any(pattern.search(text) for pattern in CIVIC_LIFESTYLE_POLICY_PATTERNS)


def is_local_welfare_donation(text: str) -> bool:
    return any(pattern.search(text) for pattern in LOCAL_WELFARE_DONATION_PATTERNS)


def has_civic_policy_market_override(text: str) -> bool:
    return any(pattern.search(text) for pattern in CIVIC_POLICY_MARKET_OVERRIDE_PATTERNS)


def is_retail_fuel_price_story(text: str) -> bool:
    return any(pattern.search(text) for pattern in RETAIL_FUEL_PRICE_PATTERNS) and not any(
        pattern.search(text) for pattern in RETAIL_FUEL_MARKET_OVERRIDE_PATTERNS
    )


def is_single_company_listing_story(text: str) -> bool:
    return any(pattern.search(text) for pattern in SINGLE_COMPANY_LISTING_PATTERNS) and not any(
        pattern.search(text) for pattern in LISTING_MARKET_OVERRIDE_PATTERNS
    )


def is_science_tech_nonmarket_story(text: str) -> bool:
    return any(pattern.search(text) for pattern in SCIENCE_TECH_NONMARKET_PATTERNS) and not any(
        pattern.search(text) for pattern in SCIENCE_TECH_MARKET_OVERRIDE_PATTERNS
    )


def is_corporate_crime_nonmarket_story(text: str) -> bool:
    return any(pattern.search(text) for pattern in CORPORATE_CRIME_NONMARKET_PATTERNS) and not any(
        pattern.search(text) for pattern in CORPORATE_CRIME_MARKET_OVERRIDE_PATTERNS
    )


def is_political_context_nonmarket_story(text: str) -> bool:
    return any(pattern.search(text) for pattern in POLITICAL_CONTEXT_NONMARKET_PATTERNS) and not any(
        pattern.search(text) for pattern in POLITICAL_CONTEXT_MARKET_OVERRIDE_PATTERNS
    )


def is_theme_or_opinion_noise(text: str) -> bool:
    return any(pattern.search(text) for pattern in THEME_OR_OPINION_NOISE_PATTERNS)


def is_personal_finance_story(text: str) -> bool:
    return re.search(
        r'(retirement|retiree|retirees|bond\s+ladder|cash\s+and\s+bond\s+ladder|portfolio|annuity|social\s+security|roth\s+ira|tax-?free|cap\s+gains|capital\s+gains|income\s+investors|income\s+etf|fang\s+income\s+etf|yield\s+strategy|robinhood\s+traders|piling\s+into|bull\s+run|too\s+good\s+to\s+be\s+true|dividend\s+stocks|lock\s+in\s+(?:yields?|5%)|for\s+your\s+portfolio|은퇴|연금|개인\s*포트폴리오)',
        text,
        re.I,
    ) is not None


def is_low_impact_policy_noise(text: str) -> bool:
    return any(pattern.search(text) for pattern in LOW_IMPACT_POLICY_NOISE_PATTERNS) and not any(
        pattern.search(text) for pattern in LOW_IMPACT_POLICY_MARKET_OVERRIDE_PATTERNS
    )


def is_local_semiconductor_policy_noise(text: str) -> bool:
    return any(pattern.search(text) for pattern in LOCAL_SEMICONDUCTOR_POLICY_NOISE_PATTERNS) and not any(
        pattern.search(text) for pattern in LOCAL_SEMICONDUCTOR_POLICY_MARKET_OVERRIDE_PATTERNS
    )


def is_market_history_or_obituary(text: str) -> bool:
    if not re.search(r'(별세|부고|향년|dies|died|dead|remembered|obituary)', text, re.I):
        return False
    return not re.search(r'(코스피|코스닥|나스닥|S&P\s*500|S&P500|다우|증시|선물|환율|유가|금리\s*(인상|인하|동결)|급등|급락|상승|하락|혼조)', text, re.I)

LISTED_COMPANY_MARKET_CONTEXT_PATTERNS = [
    re.compile(r'(실적|매출|영업이익|수출|가이던스|어닝|컨센서스|업종|섹터|주가|증시|코스피|코스닥|상승|하락|급등|급락)', re.I),
]

BELLWETHER_COMPANY_PATTERNS = [
    re.compile(r'(엔비디아|NVIDIA|NVDA|애플|Apple|AAPL|마이크로소프트|Microsoft|MSFT|알파벳|구글|Alphabet|Google|GOOG|GOOGL)', re.I),
    re.compile(r'(아마존|Amazon|AMZN|메타|Meta|META|테슬라|Tesla|TSLA|브로드컴|Broadcom|AVGO|마이크론|Micron|Intel|인텔|TSMC|AMD)', re.I),
    re.compile(r'(삼성전자|Samsung\s+Electronics|SK\s*하이닉스|하이닉스)', re.I),
]

BELLWETHER_COMPANY_MARKET_CONTEXT_PATTERNS = [
    re.compile(r'(실적|매출|영업이익|수익|가이던스|전망|어닝|컨센서스|수출|AI\s*칩|HBM|반도체)', re.I),
    re.compile(r'(주가|시총|대장주|급등|급락|상승|하락|강세|약세|반등|차익실현)', re.I),
    re.compile(r'(earnings?|revenue|sales|profit|guidance|outlook|forecast|shares?|stock|market\s*cap|chip|chips)', re.I),
    re.compile(r'(rally|rallies|rebound|rebounds|gains?|jumps?|surge|surges|drops?|dropped|falls?|slump|plunge|tumbles?|weakens?|sell-?off)', re.I),
]

SINGLE_BRAND_EVENT_PATTERNS = [
    re.compile(r'(팝업스토어|팝업\s*스토어|가봤더니|팬덤|브랜드\s*캠페인|맛집|신제품|편의점|성수)', re.I),
]


def has_bellwether_company_context(text: str) -> bool:
    return any(pattern.search(text) for pattern in BELLWETHER_COMPANY_PATTERNS) and any(
        pattern.search(text) for pattern in BELLWETHER_COMPANY_MARKET_CONTEXT_PATTERNS
    )

def market_impact_components(headline: str, source_name: str, matched_rules: list[dict[str, Any]], age_hours: float) -> dict[str, Any]:
    text = f'{headline} {source_name}'
    lower = text.lower()
    has_market_linkage = any(pattern.search(text) for pattern in MARKET_LINKAGE_PATTERNS)
    has_magnitude = any(pattern.search(text) for pattern in MAGNITUDE_PATTERNS)
    has_listed_company_context = any(pattern.search(text) for pattern in LISTED_COMPANY_MARKET_CONTEXT_PATTERNS)
    has_bellwether_context = has_bellwether_company_context(text)
    single_brand_event = any(pattern.search(text) for pattern in SINGLE_BRAND_EVENT_PATTERNS)
    local_welfare_donation = is_local_welfare_donation(text)
    civic_lifestyle_policy = is_civic_lifestyle_policy(text)
    civic_market_override = has_civic_policy_market_override(text)
    categories = {rule['category'] for rule in matched_rules}

    if local_welfare_donation:
        return {
            'marketLinkage': 0.0,
            'breadth': 0.0,
            'magnitude': 0.0,
            'timeSensitivity': 0.0,
            'sourceQualityPenalty': -40,
            'investmentAdvicePenalty': 0,
            'marketImpactScore': 0.0,
            'hasListedCompanyContext': has_listed_company_context,
            'hasBellwetherCompanyContext': has_bellwether_context,
            'singleBrandEvent': single_brand_event,
            'civicLifestylePolicy': civic_lifestyle_policy,
            'civicPolicyMarketOverride': civic_market_override,
            'localWelfareDonation': True,
        }

    if 'central_bank' in categories or 'macro' in categories:
        market_linkage = 90 if has_market_linkage else 78
        breadth = 92
    elif 'geopolitics_supply' in categories:
        market_linkage = 84 if has_market_linkage else 72
        breadth = 88
    elif 'market_event' in categories:
        market_linkage = 86 if has_market_linkage else 68
        breadth = 82
    elif 'semiconductor_bridge' in categories:
        market_linkage = 76 if has_market_linkage else 58
        breadth = 72 if has_listed_company_context else 55
    elif 'bellwether_company' in categories:
        market_linkage = 78 if has_bellwether_context else 38
        breadth = 70 if has_bellwether_context else 25
    else:
        market_linkage = 55 if has_market_linkage else 20
        breadth = 40

    if has_bellwether_context:
        market_linkage = max(market_linkage, 78)
        breadth = max(breadth, 68)

    if civic_lifestyle_policy and not civic_market_override:
        breadth = min(breadth, 10)
        market_linkage = min(market_linkage, 16)
    elif civic_lifestyle_policy and civic_market_override:
        breadth = min(breadth, 62)
        market_linkage = min(max(market_linkage, 58), 70)

    if single_brand_event and not has_listed_company_context and not has_bellwether_context:
        breadth = min(breadth, 12)
        market_linkage = min(market_linkage, 18)
    elif (has_listed_company_context or has_bellwether_context) and single_brand_event:
        breadth = max(breadth, 50)
        market_linkage = max(market_linkage, 58)

    magnitude = 72 if has_magnitude else 35 if has_market_linkage else 10
    if age_hours <= 6:
        time_sensitivity = 82
    elif age_hours <= 12:
        time_sensitivity = 68
    elif age_hours <= 24:
        time_sensitivity = 50
    else:
        time_sensitivity = 20

    source_penalty = -10 if any(pattern.search(text) for pattern in SOURCE_QUALITY_PENALTY_PATTERNS) and not has_listed_company_context else 0
    if civic_lifestyle_policy and not civic_market_override:
        source_penalty -= 18
    elif civic_lifestyle_policy and civic_market_override:
        source_penalty -= 10
    advice_penalty = -12 if any(pattern.search(text) for pattern in INVESTMENT_ADVICE_PATTERNS) else 0
    score = market_linkage * 0.40 + breadth * 0.35 + magnitude * 0.15 + time_sensitivity * 0.10 + source_penalty + advice_penalty
    return {
        'marketLinkage': round(market_linkage, 1),
        'breadth': round(breadth, 1),
        'magnitude': round(magnitude, 1),
        'timeSensitivity': round(time_sensitivity, 1),
        'sourceQualityPenalty': source_penalty,
        'investmentAdvicePenalty': advice_penalty,
        'marketImpactScore': round(max(0.0, min(100.0, score)), 1),
        'hasListedCompanyContext': has_listed_company_context,
        'hasBellwetherCompanyContext': has_bellwether_context,
        'singleBrandEvent': single_brand_event,
        'civicLifestylePolicy': civic_lifestyle_policy,
        'civicPolicyMarketOverride': civic_market_override,
        'localWelfareDonation': local_welfare_donation,
    }

ENGLISH_HEADLINE_PATTERNS = [
    (re.compile(r'^How\s+(.+?)\s+Pays\s+Friday\s+Income\s+on\s+the\s+S&P\s+500\s+With\s+a\s+0DTE\s+Covered\s+Call\s+Strategy$', re.I), r'\1, 0DTE 커버드콜 전략으로 S&P500 금요일 인컴을 지급한다는 기사'),
    (re.compile(r'^Prediction:\s*This\s+Unstoppable\s+(.+?)\s+Could\s+Crush\s+the\s+S&P\s+500\s+Over\s+the\s+Next\s+10\s+Years$', re.I), r'전망: 강세를 보이는 \1가 향후 10년 S&P500을 앞설 수 있다는 분석'),
    (re.compile(r'^Prediction:\s*(.+?)\s+Could\s+Crush\s+the\s+S&P\s+500\s+Over\s+the\s+Next\s+10\s+Years$', re.I), r'전망: \1가 향후 10년 S&P500을 앞설 수 있다는 분석'),
    (re.compile(r'^(.+?)\s+vs\.\s+(.+?):\s+Which\s+(.+?)\s+Is\s+the\s+Smarter\s+(.+?)\?$', re.I), r'\1와 \2 비교: 어느 \3이 더 나은 \4일까?'),
    (re.compile(r'^(.+?)\s+vs\.\s+(.+?):\s+Does\s+(.+?)\s+Beat\s+(.+?)\?$', re.I), r'\1와 \2 비교: \3이 \4을 앞설까?'),
    (re.compile(r'^Buckle Up:\s*(.+?)\s+Volatility\s+Incoming!?$', re.I), r'주의: \1 변동성 확대 가능성'),
    (re.compile(r'^(.+?)\s+ETF\s+Had\s+the\s+Greatest\s+5-Year\s+Return\s+of\s+All\s+Major\s+Stock\s+Market\s+ETFs\.\s+Should\s+You\s+Actually\s+Buy\s+These\s+Stocks\s+Instead\?$', re.I), r'최근 5년 수익률 상위 \1 ETF 관련 주식 매수 권유성 헤드라인'),
]

ENGLISH_TO_KOREAN_GLOSSARY = [
    ('Stock Market', '주식시장'),
    ('Major Stock Market', '주요 증시'),
    ('Semiconductor ETF', '반도체 ETF'),
    ('Semiconductor', '반도체'),
    ('Equal Weight', '동일가중'),
    ('Cap-Weighted', '시가총액가중'),
    ('Cap Weighted', '시가총액가중'),
    ('Capital Gains', '양도차익'),
    ('Cap Gains', '양도차익'),
    ('Tax-Free', '비과세'),
    ('S&P 500', 'S&P500'),
    ('Nasdaq', '나스닥'),
    ('Portfolio', '포트폴리오'),
    ('Volatility', '변동성'),
    ('Repositioning', '포지션 재조정'),
    ('Incoming', '다가오는'),
    ('Smarter', '더 나은'),
    ('Chip Bet', '반도체 선택'),
    ('ETF', 'ETF'),
    ('Stocks', '주식'),
    ('Market', '시장'),
    ('Wall Street', '월가'),
    ('Strategist', '전략가'),
    ('SpaceX', '스페이스X'),
    ('AI', 'AI'),
]

FORCED_ENGLISH_HEADLINE_TRANSLATIONS = [
    (re.compile(r'forget\s+ai\s+software.*autonomous\s+weapons', re.I), 'AI 소프트웨어보다 자율무기 투자에 자금이 몰린다는 분석'),
    (re.compile(r'us\s+markets\s+plunge.*dow\s+jones.*s&p\s*500.*nasdaq.*iran.*risk-off', re.I), '이란 위협에 위험회피 확산, 다우·S&P500·나스닥 급락'),
    (re.compile(r'first\s+trillion-dollar\s+etf.*elon\s+musk', re.I), '첫 1조달러 ETF가 시장 관심을 독점한다는 분석'),
    (re.compile(r'catastrophe\s+bonds.*uncorrelated.*macroeconomic\s+uncertainty', re.I), '거시 불확실성 속 비상관 자산으로 주목받는 재해채권'),
    (re.compile(r'xdte.*friday\s+income.*s&p\s*500.*0dte\s+covered\s+call', re.I), 'XDTE가 S&P500 0DTE 커버드콜로 주간 인컴을 지급하는 방식'),
    (re.compile(r's&p\s*500\s+lost\s+40%.*real\s+terms', re.I), 'S&P500이 실질 기준 장기 부진을 겪을 수 있다는 경고'),
    (re.compile(r'sp500\s+climbs.*futures\s+rise.*yields\s+ease', re.I), '선물 상승과 금리 완화에 S&P500이 상승 시도'),
    (re.compile(r's&p\s*500\s+selloff.*inflation\s+risk.*equity\s+multiples', re.I), '인플레이션 위험이 주가 밸류에이션을 누른다는 분석'),
    (re.compile(r'marvell.*join\s+s&p\s*500', re.I), '마벨, S&P500 지수 편입 예정'),
    (re.compile(r'2x\s+super\s+micro\s+etf.*soared', re.I), '2배 슈퍼마이크로 ETF가 단기간 급등'),
    (re.compile(r'buy\s+everything\s+ai.*aiq', re.I), 'AI 투자 열기는 기술주 쏠림을 보여주는 참고 신호'),
    (re.compile(r'energy\s+stocks\s+are\s+back.*iye', re.I), '에너지주 반등에 IYE ETF 강세'),
    (re.compile(r'tech\s+frenzy.*mgk\s+dip', re.I), '대형 기술주 조정 매수세가 이어진다는 분석'),
    (re.compile(r'equity\s+futures\s+higher.*us\s+attacks\s+on\s+iran', re.I), '이란 관련 긴장 속 미국 주가지수 선물 상승'),
    (re.compile(r'tech\s+rebound\s+lifts\s+wall\s+street.*asia.*europe', re.I), '기술주 반등에 미국 증시 선물 강세, 아시아 혼조·유럽 상승'),
    (re.compile(r'nasdaq.*s&p\s*500\s+futures\s+catch\s+breath.*fed\s+decision', re.I), '연준 결정 앞두고 미국 지수 선물이 숨 고르는 흐름'),
    (re.compile(r's&p\s*500.*nasdaq.*dow.*end\s+higher.*trump\s+signals\s+iran\s+deal', re.I), '트럼프가 이란 합의를 시사하자 S&P500·나스닥·다우 상승 마감'),
    (re.compile(r'us\s+stock\s+market\s+today.*dow\s+jumps.*s&p\s*500.*nasdaq\s+rebound', re.I), '다우 상승, S&P500·나스닥 반등 — 반도체주 회복세'),
    (re.compile(r'us\s+stock\s+market\s+today.*wall\s+street\s+rebounds.*oil\s+slides.*s&p\s*500.*nasdaq\s+rise', re.I), '유가 하락과 이란 긴장 완화 속 S&P500·나스닥 반등'),
    (re.compile(r'nasdaq.*s&p\s*500.*dow\s+futures\s+mixed.*iran.*escalation.*lifts?\s+oil\s+prices?', re.I), '이란·중동 긴장에 유가 상승 부담, 미국 지수 선물은 혼조'),
    (re.compile(r'oil\s+prices?\s+rise.*stock\s+futures\s+inch\s+higher.*iran.*airstrikes?', re.I), '미·이란 공방에 유가 상승 부담, 지수 선물은 소폭 상승'),
    (re.compile(r'top\s+brokers\s+lift\s+s&p\s*500\s+targets.*forecast\s+gains', re.I), '브로커들이 S&P500 목표치를 상향한 전망성 기사'),
    (re.compile(r'reduce\s+reliance\s+on\s+strait\s+of\s+hormuz', re.I), '호르무즈 의존도 축소 논의는 에너지 공급망 부담을 낮출 수 있음'),
    (re.compile(r'u\.s\.\s+and\s+iran\s+begin\s+peace\s+talks.*strait\s+of\s+hormuz', re.I), '미·이란 대화는 호르무즈 불확실성과 유가 부담을 낮출 수 있음'),
    (re.compile(r'oil\s+rises\s+amid\s+uncertainty\s+over\s+strait\s+of\s+hormuz', re.I), '호르무즈 불확실성은 국제유가와 물가 부담을 키움'),
    (re.compile(r'markets\s+feel\s+relief.*us\s+and\s+iran\s+agree\s+to\s+a\s+ceasefire.*violent', re.I), '휴전 합의에도 미·이란 충돌 불안은 시장 부담'),
    (re.compile(r'shares\s+to\s+open\s+higher\s+despite\s+renewed\s+us[-\s]?iran\s+tensions', re.I), '미·이란 긴장에도 증시는 상승 출발 예상'),
    (re.compile(r'while\s+the\s+world\s+scrambles\s+for\s+oil.*china\s+sits\s+on\s+full\s+tanks', re.I), '중국 원유 재고는 글로벌 유가 부담을 덜 수 있는 완화 신호'),
    (re.compile(r'mines,\s+logistics\s+and\s+deep\s+uncertainty\s+threaten\s+a\s+middle\s+east\s+oil\s+rebound', re.I), '물류·불확실성은 중동 원유 공급 회복을 늦추는 부담 신호'),
    (re.compile(r'iran\s+war\s+gets\s+hot\s+again.*trump.*iran.*oil', re.I), '이란 전쟁 긴장 재고조는 원유 리스크 부담'),
    (re.compile(r'exchange-traded\s+funds\s+rise.*us\s+equities\s+advance', re.I), '미국 증시 상승에 ETF 전반 강세'),
    (re.compile(r'stocks\s+supported\s+by\s+a\s+rebound\s+in\s+chipmakers\s+and\s+ai\s+stocks', re.I), '반도체·AI주 반등이 증시를 지지'),
]

MARKET_RELEVANCE_RULES = [
    {
        'category': 'central_bank',
        'label': '중앙은행',
        'keywords': ['fed', 'fomc', '연준', '한국은행', '기준금리', '금리', 'treasury', 'yield'],
        'tags': ['금리'],
        'target': 'bridge',
        'relatedFactors': ['macro'],
        'why': '금리가 높아지면 돈 빌리는 부담이 커져 주식에는 부담이 됩니다. 그래서 금리 뉴스는 미국·한국 온도에 같이 영향을 줍니다.',
    },
    {
        'category': 'macro',
        'label': '매크로',
        'keywords': ['cpi', 'pce', '고용', '실업', 'gdp', 'pmi', '환율', '원달러', '원/달러', '달러', 'dollar', '유가', 'wti', 'oil'],
        'tags': ['매크로'],
        'target': 'bridge',
        'relatedFactors': ['macro'],
        'why': '환율·물가·고용·유가는 돈의 흐름과 기업 비용을 바꿉니다. 숫자가 크게 바뀌면 시장 분위기도 흔들릴 수 있어요.',
    },
    {
        'category': 'market_event',
        'label': '시장이벤트',
        'keywords': ['코스피', 'kospi', '코스닥', '나스닥', 's&p', 'sp500', 's&p500', 'vix', '급락', '폭락', '급등', '반등', '상승', '하락', '순매수', '순매도', 'sidecar', '서킷브레이커'],
        'tags': ['지수'],
        'target': 'market',
        'relatedFactors': ['indices', 'news'],
        'why': '대표 지수와 외국인 수급은 시장이 실제로 오르는지 밀리는지 보여주는 직접 신호입니다.',
    },
    {
        'category': 'semiconductor_bridge',
        'label': '반도체',
        'keywords': ['반도체', 'semiconductor', 'chip', 'chips', 'hbm', 'ai', 'soxx', 'smh', '엔비디아', 'nvidia', '마이크론', 'micron', '인텔', 'intel', 'amd', '삼성전자', '하이닉스'],
        'tags': ['반도체'],
        'target': 'bridge',
        'relatedFactors': ['indices', 'news'],
        'why': '반도체는 미국 기술주와 한국 대표주를 잇는 업종입니다. 이쪽 뉴스는 두 시장 온도 차이를 설명할 때 씁니다.',
    },
    {
        'category': 'bellwether_company',
        'label': '대표기업',
        'keywords': ['엔비디아', 'nvidia', 'nvda', '애플', 'apple', 'aapl', '마이크로소프트', 'microsoft', 'msft', '구글', 'alphabet', 'google', '아마존', 'amazon', 'meta', '메타', '테슬라', 'tesla', '브로드컴', 'broadcom', '마이크론', 'micron', '인텔', 'intel', 'tsmc', 'amd', '삼성전자', '하이닉스'],
        'tags': ['대표기업'],
        'target': 'bridge',
        'relatedFactors': ['indices', 'news'],
        'why': '지수 비중이 큰 대표기업 뉴스는 개별 종목 판단이 아니라 지수·섹터 온도에 번질 수 있는 단서로 봅니다.',
    },
    {
        'category': 'geopolitics_supply',
        'label': '정책·공급망',
        'keywords': ['관세', '수출규제', '중동', '이란', '휴전', '종전', '합의', '전쟁', '제재', '공급망', '에너지', '원유', 'iran', 'ceasefire', 'truce', 'war risk', 'airstrike', 'escalation', 'hormuz', 'middle east', 'strait'],
        'tags': ['정책', '공급망'],
        'target': 'global',
        'relatedFactors': ['macro', 'news'],
        'why': '전쟁·제재·공급 문제는 유가와 기업 비용을 바꿉니다. 그래서 시장 분위기가 갑자기 조심스러워질 수 있습니다.',
    },
]


def critical_market_event(headline: str) -> tuple[bool, str | None]:
    text = headline.lower()
    if any(keyword.lower() in text for keyword in CRITICAL_NEWS_KEYWORDS):
        return True, 'critical_keyword'
    if any(pattern.search(headline) for pattern in CRITICAL_MARKET_MOVE_PATTERNS):
        return True, 'large_market_move'
    return False, None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def utc_in(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=max(1, minutes))).isoformat().replace('+00:00', 'Z')


def google_news_url(query: str, *, hl: str = 'ko', gl: str = 'KR', ceid: str = 'KR:ko') -> str:
    params = urllib.parse.urlencode({'q': query, 'hl': hl, 'gl': gl, 'ceid': ceid})
    return f'https://news.google.com/rss/search?{params}'


DEFAULT_FEEDS = [
    {
        'sourceId': 'rss:google-news:market-context-kr',
        'label': 'Google News RSS — 시장/환율/반도체',
        'region': 'KR',
        'url': google_news_url('코스피 OR 코스닥 OR 나스닥 OR S&P500 OR 원달러 OR 환율 OR 반도체 OR 외국인 OR 연준 OR FOMC OR VIX OR 유가 when:1d -부동산 -동탄 -관광 -굿즈'),
    },
    {
        'sourceId': 'rss:google-news:kr-equity-close',
        'label': 'Google News RSS — 한국장 마감/수급',
        'region': 'KR',
        'url': google_news_url('코스피 코스닥 증시 마감 외국인 기관 수급 환율 반도체 when:1d -추천주 -특징주 -부동산 -공모주'),
    },
    {
        'sourceId': 'rss:google-news:kr-market-flow',
        'label': 'Google News RSS — 한국장 수급/환율',
        'region': 'KR',
        'url': google_news_url('한국 증시 코스피 코스닥 외국인 순매수 순매도 환율 수급 when:1d -추천주 -특징주 -부동산'),
    },
    {
        'sourceId': 'rss:google-news:kr-us-market-bridge',
        'label': 'Google News RSS — 뉴욕증시/한국장 연결',
        'region': 'KR',
        'url': google_news_url('뉴욕증시 나스닥 S&P500 다우 선물 연준 금리 유가 when:1d -추천주 -부동산'),
    },
    {
        'sourceId': 'rss:google-news:market-context-us-major',
        'label': 'Google News RSS — US market translated',
        'region': 'US',
        'url': google_news_url('S&P 500 Nasdaq Dow futures Fed Treasury yield Wall Street stocks when:1d -buy -dividend -portfolio', hl='en-US', gl='US', ceid='US:en'),
    },
    {
        'sourceId': 'rss:google-news:us-stock-today',
        'label': 'Google News RSS — US stock market today',
        'region': 'US',
        'url': google_news_url('stock market today S&P 500 Nasdaq Dow Wall Street stocks futures when:1d -buy -dividend -portfolio', hl='en-US', gl='US', ceid='US:en'),
    },
    {
        'sourceId': 'rss:google-news:market-context-us-macro',
        'label': 'Google News RSS — global macro translated',
        'region': 'US',
        'url': google_news_url('Fed FOMC Treasury yield dollar index VIX oil Iran Middle East markets when:1d -buy -dividend -portfolio', hl='en-US', gl='US', ceid='US:en'),
    },
    {
        'sourceId': 'rss:google-news:us-tech-semis',
        'label': 'Google News RSS — US tech/semis translated',
        'region': 'US',
        'url': google_news_url('Nvidia chip stocks semiconductor Nasdaq S&P 500 market when:1d -buy -dividend -portfolio', hl='en-US', gl='US', ceid='US:en'),
    },
    {
        'sourceId': 'rss:google-news:us-market-leaders',
        'label': 'Google News RSS — US market leaders translated',
        'region': 'US',
        'url': google_news_url('Nvidia Apple Microsoft Tesla earnings shares Nasdaq S&P 500 market when:1d -buy -dividend -portfolio', hl='en-US', gl='US', ceid='US:en'),
    },
    {
        'sourceId': 'rss:marketwatch:topstories',
        'label': 'MarketWatch Top Stories',
        'region': 'US',
        'url': 'https://feeds.content.dowjones.io/public/rss/mw_topstories',
    },
    {
        'sourceId': 'rss:cnbc:finance',
        'label': 'CNBC Finance',
        'region': 'US',
        'url': 'https://www.cnbc.com/id/10000664/device/rss/rss.html',
    },
    {
        'sourceId': 'rss:nytimes:business',
        'label': 'New York Times Business',
        'region': 'US',
        'url': 'https://rss.nytimes.com/services/xml/rss/nyt/Business.xml',
    },
    {
        'sourceId': 'rss:yahoo-finance:us-market',
        'label': 'Yahoo Finance RSS — US market ETFs',
        'region': 'US',
        'url': 'https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,SOXX,SMH&region=US&lang=en-US',
    },
]


def clean_text(value: str | None) -> str:
    text = re.sub(r'<[^>]+>', '', value or '')
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'\s*[|｜·・:：;,\-–—]+\s*$', '', text).strip()


def has_korean(value: str) -> bool:
    return bool(re.search(r'[가-힣]', value))


def english_market_context_translation(headline: str) -> str | None:
    """Return a Korean-first market-context headline for English RSS titles.

    This is intentionally conservative. The worker stores only headline/source
    metadata, so we translate the market signal visible in the title rather than
    inventing article-body detail.
    """
    text = headline.strip()
    lower = text.lower()
    if not text or has_korean(text):
        return None

    has_sp500 = re.search(r's&p\s*500|sp500', lower) is not None
    has_nasdaq = 'nasdaq' in lower
    has_dow = re.search(r'\bdow\b|dow jones', lower) is not None
    has_wall_street = 'wall street' in lower or re.search(r'\bstocks?\b', lower) is not None
    has_futures = 'future' in lower
    has_fed = re.search(r'\bfed\b|fomc|rate decision|treasury|yield', lower) is not None
    has_dollar = re.search(r'dollar|eur/usd|sterling|pound', lower) is not None
    has_inflation = re.search(r'\bpce\b|\bcpi\b|inflation', lower) is not None
    has_oil_geo = re.search(r'iran|hormuz|middle east|ceasefire|trump.*iran|us-iran|oil|crude', lower) is not None
    has_chip = re.search(r'nvidia|chipmaker|semiconductor|micron|broadcom|ai stock|ai stocks', lower) is not None
    positive = re.search(r'edge higher|higher|climb|climbs|climbing|rise|rises|rising|rally|rallies|rebound|rebounds|advance|advances|jump|jumps|surge|surges|gain|gains', lower) is not None
    negative = re.search(r'slide|slides|sliding|slip|slips|dip|dips|dipped|drop|drops|dropped|fall|falls|falling|lower|sell-off|selloff|plunge|plunges|slump|slumps|risk-off|lost|decline|declines|tumble|tumbles|weakens|weaken', lower) is not None
    equity_subject = r'(s&p\s*500|sp500|nasdaq|dow|dow jones|wall street|stocks?|futures?|chipmakers?|semiconductors?|micron|intel|amd|nvidia)'
    equity_positive = re.search(equity_subject + r'.{0,42}(edge higher|higher|climb|climbs|rise|rises|rally|rallies|rebound|rebounds|advance|advances|jump|jumps|gain|gains)|' + r'(edge higher|higher|climb|climbs|rise|rises|rally|rallies|rebound|rebounds|advance|advances|jump|jumps|gain|gains).{0,42}' + equity_subject, lower) is not None
    equity_negative = re.search(equity_subject + r'.{0,42}(slide|slides|sliding|slip|slips|dip|dips|dipped|drop|drops|dropped|fall|falls|lower|plunge|plunges|slump|slumps|tumble|tumbles|weakens|weaken|crash|crashes)|' + r'(slide|slides|sliding|slip|slips|dip|dips|dipped|drop|drops|dropped|fall|falls|lower|plunge|plunges|slump|slumps|tumble|tumbles|weakens|weaken|crash|crashes).{0,42}' + equity_subject, lower) is not None
    broad_index_subject = r'(s&p\s*500|sp500|nasdaq|dow|dow jones|wall street|stocks?|futures?)'
    broad_index_positive = re.search(broad_index_subject + r'.{0,42}(edge higher|higher|climb|climbs|rise|rises|rally|rallies|rebound|rebounds|advance|advances|jump|jumps|gain|gains)|' + r'(edge higher|higher|climb|climbs|rise|rises|rally|rallies|rebound|rebounds|advance|advances|jump|jumps|gain|gains).{0,42}' + broad_index_subject, lower) is not None
    broad_index_negative = re.search(broad_index_subject + r'.{0,42}(slide|slides|sliding|slip|slips|dip|dips|dipped|drop|drops|dropped|fall|falls|lower|plunge|plunges|slump|slumps|tumble|tumbles|weakens|weaken|crash|crashes)|' + r'(slide|slides|sliding|slip|slips|dip|dips|dipped|drop|drops|dropped|fall|falls|lower|plunge|plunges|slump|slumps|tumble|tumbles|weakens|weaken|crash|crashes).{0,42}' + broad_index_subject, lower) is not None

    if 'eur/usd weekly outlook' in lower and 'fomc' in lower:
        return 'FOMC 이후 달러 강세 가능성은 환율 부담 신호'
    if re.search(r'ship\s+attack|shipping[-\s]?insurance|war[-\s]?risk\s+premiums?', lower):
        return '이란 선박 공격은 해상보험·중동 리스크 부담'
    if re.search(r'retaliatory\s+strike|strike\s+on\s+iran', lower) and re.search(r'oil|crude', lower):
        return '미국의 이란 보복 공습에 유가 상승 부담'
    if re.search(r'gold|silver', lower) and negative and has_fed:
        return '연준 신호에 금·은 가격 약세, 안전자산 수요 약화 확인'
    if re.search(r'bond\s+yields?.{0,24}falling|yields?.{0,24}falling', lower) and has_inflation:
        return '물가 반등은 금리가 오래 높게 남을 수 있다는 부담'
    if has_inflation and has_chip and re.search(r'micron|apple|chip\s+stocks?|semiconductor|technology\s+stocks?', lower):
        return 'PCE 물가 부담과 반도체·대형 기술주 약세 확인'
    if re.search(r's&p\s*500|nasdaq', lower) and re.search(r'losing\s+momentum|under\s+pressure|ai\s+stocks?', lower):
        return 'AI주 압박에 S&P500·나스닥 약세 압력'
    if has_inflation and re.search(r'hotter[-\s]?than[-\s]?expected|hot|high(?:er|est)?|sticky|elevated', lower) and (positive or has_wall_street):
        return '미국 지수 선물 반등 속 물가 상승 부담 확인'
    if has_futures and (has_sp500 or has_nasdaq or has_dow) and broad_index_positive and not broad_index_negative:
        return '미국 주요 지수 선물이 반등하며 개장 전 부담을 덜어내는지 확인'
    if has_futures and (has_sp500 or has_nasdaq or has_dow) and broad_index_negative:
        return '미국 주요 지수 선물이 약세를 보이며 개장 전 부담을 확인'
    if has_futures and (has_sp500 or has_nasdaq or has_dow) and positive and has_fed:
        return '연준 금리 결정을 앞두고 미국 주요 지수 선물이 소폭 상승'
    if has_futures and has_oil_geo and re.search(r'(oil|crude).{0,24}(rise|rises|rising|higher|surge|surges)|(rise|rises|rising|higher|surge|surges|lift|lifts|lifted).{0,24}(oil|crude)', lower):
        if re.search(r'mixed', lower):
            return '이란·중동 긴장에 유가 상승 부담, 미국 지수 선물은 혼조'
        if broad_index_positive or positive:
            return '중동 긴장에 유가 상승 부담, 지수 선물은 소폭 상승'
        return '중동 긴장에 유가 상승 부담'
    if has_futures and has_oil_geo:
        return '중동 이슈에 지수 선물과 유가 경로를 함께 확인'
    if has_oil_geo and re.search(r'(oil\s+prices?.{0,24}return(?:s|ed)?\s+to\s+pre[-\s]?war\s+levels?|return(?:s|ed)?\s+to\s+pre[-\s]?war\s+levels?.{0,24}oil|pre[-\s]?war\s+levels?)', lower):
        return '유가가 전쟁 전 수준으로 돌아오며 비용 부담 완화'
    if has_oil_geo and positive and has_wall_street:
        return '이란·중동 긴장 완화와 함께 미국 증시 상승 흐름 확인'
    if has_oil_geo and negative and has_wall_street:
        return '이란·중동 이슈 속 미국 증시 약세 부담 확인'
    if has_oil_geo and re.search(r'(oil|crude).{0,28}(slide|slides|sliding|fall|falls|drop|drops|lower)|(slide|slides|sliding|fall|falls|drop|drops|lower).{0,28}(oil|crude)', lower):
        return '유가 하락은 물가·비용 부담을 낮추는 완화 신호'
    if has_oil_geo and re.search(r'oil|crude|hormuz', lower):
        if not re.search(r'tension|risk|pressure|war|hormuz|threat|threaten|uncertainty', lower):
            return '중동·유가 이슈는 시장 분위기를 바꿀 수 있는 참고 신호'
        return '중동 긴장은 유가와 시장 불안을 키울 수 있음'
    if has_oil_geo:
        return '중동 이슈는 시장을 조심스럽게 만드는 참고 신호'
    if has_inflation:
        return '미국 물가 지표는 금리 부담을 키울 수 있는 신호'
    if has_dollar and has_fed:
        return '연준·금리 신호는 달러와 환율 부담을 바꿀 수 있음'
    if has_fed:
        return '연준·미국 금리 흐름 확인'
    if has_chip and equity_negative:
        return '반도체·AI주 약세가 미국 기술주 흐름에 주는 부담 확인'
    if has_chip and equity_positive:
        return '반도체·AI주 반등은 미국 기술주 부담을 덜어주는 신호'
    if has_nasdaq and has_sp500 and has_dow and negative and positive:
        return '나스닥·S&P500과 다우 흐름이 엇갈리며 미국장 온도 차이를 확인'

    subjects: list[str] = []
    if has_sp500:
        subjects.append('S&P500')
    if has_nasdaq:
        subjects.append('나스닥')
    if has_dow:
        subjects.append('다우')
    if 'vix' in lower:
        subjects.append('VIX')
    if subjects:
        subject = '·'.join(subjects[:3])
        if re.search(r'could|ahead|forecast|prediction|critical crossroads|signal more losses|전망|가능', lower):
            return f'{subject} 추가 변동 가능성은 배경 뉴스로 반영'
        if equity_positive or (positive and not equity_negative):
            return f'{subject} 상승 흐름은 미국장 부담을 덜 수 있는 신호'
        if equity_negative or negative:
            return f'{subject} 약세 흐름은 미국장 부담을 키울 수 있음'
        return f'{subject}은 방향보다 가격 위치를 볼 뉴스'
    return None


def koreanize_english_headline(headline: str) -> str | None:
    if not headline or has_korean(headline):
        return None
    text = headline.strip()
    for pattern, translated in FORCED_ENGLISH_HEADLINE_TRANSLATIONS:
        if pattern.search(text):
            return translated
    market_translation = english_market_context_translation(text)
    if market_translation:
        return market_translation
    for pattern, replacement in ENGLISH_HEADLINE_PATTERNS:
        if pattern.search(text):
            translated = pattern.sub(replacement, text)
            for english, korean in ENGLISH_TO_KOREAN_GLOSSARY:
                translated = re.sub(re.escape(english), korean, translated, flags=re.I)
            return translated
    translated = text
    for english, korean in ENGLISH_TO_KOREAN_GLOSSARY:
        translated = re.sub(re.escape(english), korean, translated, flags=re.I)
    translated = re.sub(r'\bthe\s+', '', translated, flags=re.I)
    translated = re.sub(r'([가-힣])s\b', r'\1', translated)
    translated = re.sub(r'\bCould\b', '가능성이 있다는 분석', translated, flags=re.I)
    translated = re.sub(r'\bPrediction:\s*', '전망: ', translated, flags=re.I)
    translated = re.sub(r'\bOver\s+the\s+Next\s+10\s+Years\b', '향후 10년', translated, flags=re.I)
    translated = re.sub(r'\bPays\s+Friday\s+Income\b', '금요일 인컴 지급', translated, flags=re.I)
    translated = re.sub(r'\bCovered\s+Call\s+Strategy\b', '커버드콜 전략', translated, flags=re.I)
    # If the headline still has no Korean, do not expose a generic "headline" placeholder.
    # Return None so the caller can drop it from the user-facing cache instead.
    if not has_korean(translated):
        return None
    # User-facing display headlines must not leak untranslated English sentences.
    # Keep ticker/product names such as S&P500, ETF, AI, VIX, 0DTE, XDTE, QQQ, SPY, SOXX, SMH,
    # but collapse any remaining long English phrase into a Korean-safe label.
    leftover_words = re.findall(r'\b[A-Za-z][A-Za-z]{2,}\b', translated)
    allowed = {'ETF', 'AI', 'VIX', 'XDTE', 'QQQ', 'SPY', 'SOXX', 'SMH', 'DTE'}
    if len([word for word in leftover_words if word.upper() not in allowed]) >= 4:
        return None
    return translated


def cause_aware_display_headline(headline: str, display_headline: str | None) -> str | None:
    """Return a user-facing headline that names the market-temperature signal.

    The cache only stores public headline metadata, so this function does not
    infer article-body facts. It rewrites obvious headline-level signals into
    a clearer "why this matters" sentence while preserving the original
    headline separately.
    """
    visible = (display_headline or headline or '').strip()
    raw = ' '.join(part for part in [headline, visible] if part).strip()
    if not raw:
        return visible or None

    has_fx = re.search(r'(환율|원[·\s/-]?달러|달러[·\s/-]?원|고환율|usd/krw)', raw, re.I)
    has_high_fx = re.search(r'(15\d{2}|1,5\d{2}|1천\s*5백|1,600|1600|고환율)', raw, re.I)
    fx_falling = re.search(r'(↓|하락|내림|낮아|떨어|0\.\d+%↓|lower|falls?|drops?)', raw, re.I)
    if has_fx and has_high_fx:
        if fx_falling:
            return '환율은 내려도 1,500원대라 국내시장 부담'
        return '환율 1,500원대는 국내시장 부담'
    if has_fx and explicit_fx_relief_signal(raw):
        return '환율 하락은 한국장 수급 부담을 덜 수 있음'

    if re.search(r'(이란|중동|iran|middle\s*east).{0,32}(무력\s*공방|공습|충돌|긴장|불안|살얼음판)|(무력\s*공방|공습|충돌|긴장|불안|살얼음판).{0,32}(이란|중동|iran|middle\s*east)', raw, re.I):
        if re.search(r'(코스피|코스닥|나스닥|S&P\s*500|S&P500|증시|지수|시장)', raw, re.I):
            return '중동 긴장은 지수 변동성 부담으로 이어질 수 있음'

    has_oil = re.search(r'(유가|원유|브렌트|wti|crude|oil|석유)', raw, re.I)
    has_food_or_inflation = re.search(r'(먹거리|물가|인플레이션|비용|cpi|pce)', raw, re.I)
    if has_oil and (oil_relief_signal(raw) or re.search(r'(유가|원유|브렌트|wti|crude|oil|석유).{0,24}(내렸|내려|내리|하락|낮아)', raw, re.I)):
        if has_food_or_inflation and re.search(r'(쑥|상승|높|고점|부담|3\.\d+%)', raw, re.I):
            return '유가는 내려도 물가 부담은 아직 남아 있음'
        return '유가 하락은 물가·비용 부담 완화 신호'
    if has_oil and oil_burden_signal(raw):
        return '유가 상승은 물가·비용 부담을 키울 수 있음'

    if re.search(r'(반도체).{0,32}(1천조|1000조|투자\s*공개|투자\s*계획|대규모\s*투자)', raw, re.I):
        return '반도체 대규모 투자 계획은 한국 성장주 참고 신호'

    return visible or None


def split_google_news_source(title: str, fallback_source: str) -> tuple[str, str]:
    if ' - ' not in title:
        return title, fallback_source
    headline, source = title.rsplit(' - ', 1)
    return headline.strip() or title, source.strip() or fallback_source


def looks_suspicious_headline(title: str) -> bool:
    # Keep the QA feed useful but avoid obviously implausible market-index glitches.
    if re.search(r'코스피[^\d]{0,12}[7-9][,\d]{3,}', title):
        return True
    if re.search(r'코스닥[^\d]{0,12}[3-9][,\d]{3,}', title):
        return True
    # A single RSS headline claiming a domestic broad index moved 5%+ is too risky
    # for a small preview card unless we add source consensus later.
    if re.search(r'(코스피|코스닥)[^\n]{0,24}([5-9](?:\.\d+)?)%[^\n]{0,12}(급등|급락|폭등|폭락)', title):
        return True
    if any(token in title for token in ['8천피', '8천선', '8000선', '8,000선', '9천피', '9천선', '9000선', '9,000선', '1만피', '1만선', '1만 시대', '1만시대', '10000선', '10,000선', '7% 넘게 급등']):
        return True
    if re.search(r'코스피.{0,28}(1\s*만|10,000|10000|[89]\s*천|[89],000)|(?:1\s*만|10,000|10000|[89]\s*천|[89],000).{0,28}코스피', title):
        return True
    # Avoid user-facing wording that our investment-action text guard flags.
    if any(token in title for token in ['매수', '매도', '추격매수']):
        return True
    return False


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def headline_tone(headline: str) -> str:
    text = headline.lower()
    if is_outlook_commentary(headline):
        return 'neutral'
    if re.search(r'(코스피|코스닥|나스닥|뉴욕증시|증시|지수|선물|wall street|stocks?)', headline, re.I):
        if re.search(r'(급등|상승|반등|회복|rally|gain|higher|climb|climbs).{0,24}(후|뒤|이후|after).{0,24}(약세|하락|차익실현|밀려|내림|lower|drop|slip)', headline, re.I):
            return 'negative'
        if (
            re.search(r'(급락|폭락|하락|약세|빠졌던|밀렸던|떨어졌던|plunge|drop|slump).{0,30}(딛고|뒤|후|이후|만에|하루\s*만에|after).{0,42}(반등|회복|상승|급등|강세|rebound|recover|rally|gain|climb|climbs)', headline, re.I)
            or re.search(r'(반등|회복|상승\s*마감|강세\s*마감|급등|rebound|recover|rally|gain|climb|climbs).{0,24}(\d[\d,.]*선|마감|회복|close)', headline, re.I)
        ):
            return 'positive'
    if any(token in headline for token in ['종전', '휴전', '합의', '환호']) or any(token in text for token in ['ceasefire', 'truce', 'deal coming soon', 'deal signed']):
        return 'positive'
    if any(token in headline for token in ['급등락', '널뛰기', '현기증', '공포', '투매', '하락', '약세', '급락', '폭락', '부담']) or any(token in text for token in ['fall', 'drop', 'dropped', 'dip', 'dips', 'dipped', 'slide', 'slides', 'sliding', 'lower', 'risk', 'selloff', 'volatility', 'crash', 'tumble', 'weakens', 'weaken', 'under pressure', 'losing momentum']):
        return 'negative'
    if any(token in headline for token in ['상승', '강세', '반등', '급등']) or any(token in text for token in ['rally', 'rallies', 'rise', 'rises', 'gain', 'gains', 'higher', 'rebound', 'rebounds', 'climb', 'climbs']):
        return 'positive'
    return 'neutral'


def market_burden_tone(headline: str, fallback: str | None = None) -> str:
    if is_outlook_commentary(headline):
        return 'neutral'
    text = headline.lower()
    up = re.search(r'(급등|상승|오름|올랐|강세|↑|\brise\b|\brises\b|\brising\b|\bhigher\b|\bsurge\b|\bsurges\b|\bjump\b|\bjumps\b|\bgain\b|\bgains\b|\brally\b|\brallies\b|\brebound\b|\brebounds\b|\bclimb\b|\bclimbs\b|\bclimbing\b)', text, re.I)
    down = re.search(r'(급락|하락|내림|내렸|떨어|약세|↓|\bfall\b|\bfalls\b|\bfalling\b|\bdip\b|\bdips\b|\bdipped\b|\bdrop\b|\bdrops\b|\bdropped\b|\bslide\b|\bslides\b|\bsliding\b|\blower\b|\bslip\b|\bslips\b|\bdecline\b|\bdeclines\b|\bplunge\b|\bplunges\b|\bcrash\b|\bcrashes\b|\bcrashed\b|\btumble\b|\btumbles\b|\bweakens\b|\bweaken\b)', text, re.I)
    has_oil = re.search(r'(유가|원유|브렌트|wti|crude|oil)', text, re.I)
    has_fx = re.search(r'(환율|원/달러|원달러|usd/krw|달러|dollar|원화)', text, re.I)
    has_rate = re.search(r'(금리|10년물|treasury|yield|rate)', text, re.I)
    has_volatility = re.search(r'(vix|변동성|시장 불안)', text, re.I)
    has_index = re.search(r'(코스피|코스닥|나스닥|nasdaq|s&p|s\s*p\s*500|sp500|dow|다우|지수|선물|futures?|뉴욕증시|증시|주가|wall street|stocks?)', text, re.I)
    has_bellwether_context = has_bellwether_company_context(headline)
    index_subject = r'(코스피|코스닥|나스닥|nasdaq|s&p|s\s*p\s*500|sp500|dow|다우|지수|선물|futures?|뉴욕증시|증시|주가|wall street|stocks?)'
    index_down = re.search(index_subject + r'.{0,42}(급락|하락|약세|폭락|slide|slides|sliding|slip|slips|dip|dips|dipped|drop|drops|dropped|fall|falls|lower|plunge|plunges|slump|slumps|tumble|tumbles|weakens|weaken|crash|crashes)|' + r'(급락|하락|약세|폭락|slide|slides|sliding|slip|slips|dip|dips|dipped|drop|drops|dropped|fall|falls|lower|plunge|plunges|slump|slumps|tumble|tumbles|weakens|weaken|crash|crashes).{0,42}' + index_subject, text, re.I)
    index_up = re.search(index_subject + r'.{0,42}(급등|상승|반등|회복|강세|higher|climb|climbs|rise|rises|rally|rallies|rebound|rebounds|advance|advances|gain|gains)|' + r'(급등|상승|반등|회복|강세|higher|climb|climbs|rise|rises|rally|rallies|rebound|rebounds|advance|advances|gain|gains).{0,42}' + index_subject, text, re.I)
    explicit_index_pressure = has_index and re.search(r'losing\s+momentum|under\s+pressure|압박|둔화|약세\s*압력', text, re.I)
    explicit_index_burden = bool(index_down or explicit_index_pressure)
    explicit_index_relief = bool(index_up)
    explicit_index_oil_relief = (
        explicit_index_relief
        and re.search(r'(falling\s+oil\s+prices?|oil\s+prices?\s+(?:fall|falls|drop|drops|plunge|plunges|lower)|oil\s+(?:fall|falls|drop|drops|plunge|plunges|lower)|crude\s+(?:falls|drops|plunges|lower)|유가\s*하락)', text, re.I)
        and not re.search(r'(?:nasdaq|s&p|s\s*p\s*500|sp500|dow|다우|나스닥|지수|futures?|선물)(?:[\s,&·-]+(?:futures?|선물))?\s*(?:slide|slides|sliding|turn\s*red|dip|dips|drop|drops|fall|falls|lower|crash|crashes|하락|약세|급락)', text, re.I)
    )
    explicit_index_fade = re.search(r'(급등|상승|반등|회복|rally|gain|higher|climb|climbs).{0,24}(후|뒤|이후|after).{0,24}(약세|하락|차익실현|밀려|내림|lower|drop|slip)', text, re.I)
    explicit_index_rebound = has_index and (
        re.search(r'(급락|폭락|하락|약세|빠졌던|밀렸던|떨어졌던|plunge|drop|slump).{0,30}(딛고|뒤|후|이후|만에|하루\s*만에|after).{0,42}(반등|회복|상승|급등|강세|rebound|recover|rally|gain|climb|climbs)', text, re.I)
        or re.search(r'(반등|회복|상승\s*마감|강세\s*마감|급등|rebound|recover|rally|gain|climb|climbs).{0,24}(\d[\d,.]*선|마감|회복|close)', text, re.I)
    )
    dampened_burden = dampened_burden_signal(text)
    explicit_rate_burden = re.search(r'(금리|10년물|국채|수익률|treasury|yield|rate).{0,18}(부담|공포|우려|상승|급등|높|고공|higher|rise|rises|rising|jump|surge)|(부담|공포|우려).{0,18}(금리|10년물|국채|수익률|treasury|yield|rate)', text, re.I) and not dampened_burden
    explicit_rate_relief = re.search(r'(금리|10년물|국채|수익률|treasury|yield|rate).{0,18}(완화|하락|인하|내림|낮아|ease|eases|fall|falls|drop|drops|lower|decline)|(완화|하락|인하|내림|낮아|ease|fall|drop|lower).{0,18}(금리|10년물|국채|수익률|treasury|yield|rate)', text, re.I)
    explicit_rate_hike_expectation_relief = re.search(r'(금리\s*인상\s*기대|rate\s*hike\s*expectations?).{0,24}(낮아|하락|후퇴|완화|줄|식|decline|declines|fall|falls|drop|drops|ease|eases|cool)', text, re.I)
    explicit_inflation_burden = inflation_stress_signal(text)
    title_burden = re.search(r'(부담|공포|악재|위험회피|불확실|급락|폭락|약세|하락|투매|↓|\bsell-?off\b|\bplunge\b|\bslump\b|\bcrash(?:es|ed)?\b|\brisk-?off\b|\bpressure\b|\bfear\b)', text, re.I) and not dampened_burden
    explicit_company_burden = has_bellwether_context and (down or title_burden)
    explicit_company_relief = has_bellwether_context and up and not title_burden
    explicit_fx_burden = fx_or_foreign_balance_stress(text)
    explicit_fx_relief = explicit_fx_relief_signal(text)
    explicit_oil_relief = oil_relief_signal(text)
    explicit_oil_burden = oil_burden_signal(text)
    explicit_geo_relief = (
        re.search(r'(이란|중동|호르무즈|iran|hormuz|middle\s*east).{0,48}(완화|종전|휴전|협상|합의|긴장\s*완화|de-?escalat|ceasefire|truce|deal|talks?)', text, re.I)
        or re.search(r'(완화|종전|휴전|협상|합의|긴장\s*완화|de-?escalat|ceasefire|truce|deal|talks?).{0,48}(이란|중동|호르무즈|iran|hormuz|middle\s*east)', text, re.I)
    )
    risk_appetite_context = re.search(r'(위험\s*(선호|자산)|risk\s*(appetite|assets?))', text, re.I)
    explicit_geo_supply_burden = (
        re.search(
            r'(이란|중동|호르무즈|해상보험|선박|유조선|해협|iran|hormuz|middle\s*east|shipping|ship|tanker|strait|insurance).{0,48}(공격|리스크|위험|불안|긴장|부담|차질|봉쇄|충격|war\s*risk|risk|attack|strike|tension|disruption|blockade|premium|shock)|'
            r'(공격|리스크|위험|불안|긴장|부담|차질|봉쇄|충격|war\s*risk|risk|attack|strike|tension|disruption|blockade|premium|shock).{0,48}(이란|중동|호르무즈|해상보험|선박|유조선|해협|iran|hormuz|middle\s*east|shipping|ship|tanker|strait|insurance)',
            text,
            re.I,
        )
        and not explicit_oil_relief
        and not explicit_geo_relief
        and not risk_appetite_context
        and not dampened_burden
    )
    explicit_foreign_flow_burden = re.search(r'(외국인|외인|外人|foreigners?).{0,36}(주식|증시|equity|stock)?.{0,24}(내다팔|팔|매도|순매도|sell|sold|selling)|(주식|증시|equity|stock).{0,24}(내다팔|팔|매도|순매도|sell|sold|selling).{0,36}(외국인|외인|外人|foreigners?)|(외국인|외인|外人|foreigners?).{0,30}(리밸런싱|rebalanc(?:e|ing))', text, re.I)

    if explicit_index_fade:
        return 'negative'
    if explicit_fx_relief:
        return 'positive'
    if explicit_fx_burden:
        return 'negative'
    if explicit_geo_relief and (explicit_index_relief or up or explicit_oil_relief) and not (explicit_index_burden or title_burden or explicit_inflation_burden or explicit_fx_burden or explicit_oil_burden):
        return 'positive'
    if explicit_foreign_flow_burden:
        return 'negative'
    if explicit_geo_relief and explicit_oil_relief and not (explicit_index_burden or explicit_inflation_burden or explicit_fx_burden or explicit_oil_burden):
        return 'positive'
    if explicit_index_relief and re.search(
        r'(긴장|위험|리스크|불안|tension|risk).{0,24}(에도|불구|despite).{0,54}(상승|강세|출발|higher|open\s+higher|edge\s+higher|rise|rises)|'
        r'(despite).{0,42}(tension|risk).{0,54}(higher|open\s+higher|rise|rises)',
        text,
        re.I,
    ):
        return 'positive'
    if title_burden and not explicit_oil_relief and re.search(r'(이란|중동|호르무즈|전쟁|휴전|충돌|공습|iran|hormuz|middle\s*east|ceasefire|airstrike)', text, re.I):
        return 'negative'
    if explicit_geo_supply_burden:
        return 'negative'
    if explicit_index_rebound:
        return 'positive'
    if explicit_index_oil_relief:
        return 'positive'
    if has_index and re.search(r'(급락|폭락|하락|약세|plunge|drop|slump|crash(?:es|ed)?).{0,24}(에도|불구|despite).{0,54}(강세|상승|반등|회복|rally|rebound|recover|gain|higher|climb)', text, re.I):
        return 'positive'
    if explicit_index_burden:
        return 'negative'
    if explicit_foreign_flow_burden:
        return 'negative'
    if explicit_inflation_burden:
        return 'negative'
    if explicit_rate_hike_expectation_relief and not explicit_index_burden:
        return 'positive'
    if explicit_rate_burden:
        return 'negative'
    if explicit_rate_relief and not title_burden and not explicit_index_burden:
        return 'positive'
    if explicit_index_relief and not title_burden:
        return 'positive'
    if explicit_company_burden:
        return 'negative'
    if explicit_company_relief:
        return 'positive'
    if has_fx:
        if explicit_fx_relief:
            return 'positive'
        if explicit_fx_burden:
            return 'negative'
    if has_oil:
        if down or explicit_oil_relief:
            return 'positive'
        if up or explicit_oil_burden:
            return 'negative'
    if has_rate:
        if re.search(r'(금리|10년물|국채|수익률|treasury|yield|rate).{0,18}(상승|급등|높|고공|higher|rise|rises|rising|jump|surge)', text, re.I):
            return 'negative'
        if re.search(r'(금리|10년물|국채|수익률|treasury|yield|rate).{0,18}(하락|인하|내림|낮아|ease|eases|fall|falls|drop|drops|lower|decline)', text, re.I) and not explicit_index_burden:
            return 'positive'
    if has_volatility:
        if up:
            return 'negative'
        if down:
            return 'positive'
    if has_index:
        if down:
            return 'negative'
        if up:
            return 'positive'
    return 'neutral'


def fx_or_foreign_balance_stress(text: str) -> bool:
    return bool(re.search(
        r'(15\d{2}|1,5\d{2}|고환율|금융위기\s*후\s*최고|외환위기\s*후\s*최고|달러\s*강세|원화\s*약세|'
        r'환율.{0,24}(급등|상승|고공|부담|최고|불안|불확실)|usd/krw.{0,12}(higher|rise)|'
        r'(원[·\s/-]?달러|달러[·\s/-]?원).{0,30}(치솟|급등|상승|고공|최고|위협|외환위기|금융위기|불안|부담)|'
        r'(치솟|급등|상승|고공|최고|위협|외환위기|금융위기|불안|부담).{0,30}(원[·\s/-]?달러|달러[·\s/-]?원)|'
        r'환율\s*안정.{0,24}(무소용|먹히지|안\s*먹)|'
        r'구두\s*개입.{0,24}(전혀\s*안|안\s*먹|먹히지|실패|무력)|'
        r'(정책\s*[·ㆍ]\s*환율|정책·환율|환율).{0,24}(불확실|불안|부담|방어\s*총력|개입\s*총력)|'
        r'(고환율|환율|원화|달러).{0,30}(방어\s*총력|개입\s*총력|방어\s*강화|방어\s*나서)|'
        r'(원화|won|krw).{0,30}(약세|불확실|불안|부담|역설|갇힌|리밸런싱|rebalanc(?:e|ing))|'
        r'(외국인|외인|外人|foreigners?).{0,36}(주식|증시|equity|stock)?.{0,24}(내다팔|팔|매도|순매도|sell|sold|selling|리밸런싱|rebalanc(?:e|ing))|'
        r'(주식|증시|equity|stock).{0,24}(내다팔|팔|매도|순매도|sell|sold|selling).{0,36}(외국인|외인|外人|foreigners?)|'
        r'(리밸런싱|rebalanc(?:e|ing)).{0,30}(외국인|외인|外人|원화|won|krw))',
        text,
        re.I,
    ))


def explicit_fx_relief_signal(text: str) -> bool:
    if re.search(r'(무소용|치솟|위협|외환위기|금융위기|원화\s*약세|달러\s*강세)', text, re.I):
        return False
    return bool(re.search(
        r'(달러\s*약세|원화.{0,14}(강세|안정|진정)|환율.{0,14}(급락|하락|내림|낮아|진정|안정)|고환율.{0,10}(진정|안정)|고환율\s*부담\s*완화|수급\s*부담\s*완화|외국인.{0,24}(순매수|유입|매수세)|usd/krw.{0,12}(lower|fall|drop))',
        text,
        re.I,
    ))


def oil_relief_signal(text: str) -> bool:
    oil = r'(유가|원유|브렌트|wti|crude|oil|석유)'
    relief = (
        r'(하락|급락|내림|낮아|안정|전쟁\s*(?:이전|전)|이전\s*수준|pre[-\s]?war\s*(?:levels?)?|'
        r'최고(?:가|가격)?(?:\s*전망)?\s*하향|가격\s*(?:전망\s*)?하향|고점\s*(?:전망\s*)?하향|피크\s*(?:전망\s*)?하향|'
        r'프리미엄\s*(?:축소|해소)|부담\s*완화|공급\s*(?:불안|차질).{0,12}(?:완화|해소)|'
        r'예상보다\s*빠른\s*회복|회복|재개|정상화|falls?|drops?|lower|eases?|de-?escalat|resume|resumes|reopen)'
    )
    return bool(re.search(oil + r'.{0,48}' + relief + r'|' + relief + r'.{0,48}' + oil, text, re.I))


def oil_burden_signal(text: str) -> bool:
    if oil_relief_signal(text):
        return False
    oil = r'(유가|원유|브렌트|wti|crude|oil|석유)'
    burden = (
        r'(상승|급등|오름|공급\s*(?:차질|불안)|호르무즈.{0,16}(폐쇄|봉쇄|위협)|'
        r'전쟁.{0,18}(확대|격화|고조)|확전|제재|긴장.{0,18}(고조|확대)|pull(?:ing)?\s+oil\s+prices?\s+up|lift(?:s|ed|ing)?\s+oil\s+prices?|prices?\s+up|risk|pressure|tension|sanction)'
    )
    return bool(re.search(oil + r'.{0,48}' + burden + r'|' + burden + r'.{0,48}' + oil, text, re.I))


def dampened_burden_signal(text: str) -> bool:
    return bool(re.search(r'(부담|pressure).{0,12}(관망|제한|완화|둔화|낮아|줄|덜|limited|contained|eases?|wanes?)', text, re.I))


def inflation_stress_signal(text: str) -> bool:
    if re.search(r'(물가|인플레이션|비용|inflation).{0,24}(부담|압력|고점|상승|높|쑥|stress|pressure)', text, re.I):
        return True
    return bool(re.search(
        r'(pce|cpi|물가|인플레이션|inflation).{0,48}('
        r'\d+(?:\.\d+)?%?\s*(?:↑|\+)|상승|급등|높|고공|고점|최고(?:치|가)?|가속|'
        r'accelerat(?:e|ed|es|ing)|rose|rises|rising|higher|high(?:est)?|elevated|sticky|hot|pops?|popped|pushed\s+up|'
        r'hits?\s+(?:a\s+)?(?:record|new|multi[-\s]?year|\d+[-\s]?year|one[-\s]?year|two[-\s]?year|three[-\s]?year|four[-\s]?year|five[-\s]?year|six[-\s]?year|seven[-\s]?year|eight[-\s]?year|nine[-\s]?year|ten[-\s]?year)\s+high'
        r')|('
        r'상승|급등|높|고공|고점|최고(?:치|가)?|가속|accelerat(?:e|ed|es|ing)|rose|rises|rising|higher|high(?:est)?|elevated|sticky|hot|pops?|popped|pushed\s+up'
        r').{0,48}(pce|cpi|물가|인플레이션|inflation)|'
        r'(pce|cpi|물가|인플레이션|inflation).{0,64}(유가\s*하락\s*미반영|oil\s+(?:fall|falls|drop|drops|decline|declines).{0,24}not\s+reflected)',
        text,
        re.I,
    ))


def target_from_headline(headline: str, default_target: str) -> str:
    text = headline.lower()
    if (
        any(token in headline for token in ['코스피', '코스닥', '원/달러', '원·달러', '원달러', '환율', '삼성전자', '하이닉스', '韓반도체'])
        or FOREIGN_FLOW_MARKET_PATTERN.search(headline)
    ):
        return 'kr'
    if any(token in text for token in ['s&p', 'sp500', 'nasdaq', 'qqq', 'spy', 'fed', 'fomc', 'treasury', 'dow', 'nvidia', 'apple', 'microsoft', 'tesla', 'amazon', 'meta']) or any(token in headline for token in ['나스닥', '뉴욕증시', '엔비디아', '애플', '마이크로소프트', '테슬라']):
        return 'us'
    return default_target


def keyword_matches_headline(keyword: str, headline_lower: str) -> bool:
    normalized = keyword.lower()
    if not normalized:
        return False
    # Short English tokens such as "ai", "fed", "oil", or "rate" must match as
    # words. Plain substring matching lets unrelated words pass the market filter.
    if re.fullmatch(r'[a-z0-9&./\s+-]+', normalized):
        pattern = r'(?<![a-z0-9])' + re.escape(normalized).replace(r'\ ', r'\s+') + r'(?![a-z0-9])'
        return re.search(pattern, headline_lower, re.I) is not None
    return normalized in headline_lower


def rule_matches_headline(rule: dict[str, Any], headline: str, headline_lower: str) -> bool:
    if any(keyword_matches_headline(keyword, headline_lower) for keyword in rule['keywords']):
        return True
    if rule['category'] == 'market_event' and FOREIGN_FLOW_MARKET_PATTERN.search(headline):
        return True
    return False


def classify_relevance(headline: str, source_name: str, published_at: str | None) -> tuple[dict[str, Any] | None, str]:
    source_lower = source_name.lower()
    headline_lower = headline.lower()
    full_text = f'{headline} {source_name}'
    if any(hint.lower() in source_lower for hint in EXCLUDED_SOURCE_HINTS):
        return None, 'SOURCE_LOW_RELEVANCE'
    if is_personal_finance_story(full_text):
        return None, 'PERSONAL_FINANCE_NOT_MARKET_TEMPERATURE'
    if any(hint.lower() in headline_lower for hint in EXCLUDED_HEADLINE_HINTS):
        return None, 'INVESTMENT_ACTION_OR_SINGLE_STOCK_NOISE'
    if ('?' in headline or 'should you' in headline_lower) and any(token in headline_lower for token in ['buy', 'stocks instead', 'smarter']):
        return None, 'INVESTMENT_ACTION_OR_SINGLE_STOCK_NOISE'
    if '반도체' in headline and any(hint in headline_lower for hint in ['집값', '부동산', '아파트', 'gtx']):
        return None, 'REAL_ESTATE_NOT_MARKET_TEMPERATURE'
    if is_local_welfare_donation(full_text):
        return None, 'LOCAL_WELFARE_DONATION_NOT_MARKET_TEMPERATURE'
    if is_civic_lifestyle_policy(headline) and not has_civic_policy_market_override(headline):
        return None, 'CIVIC_LIFESTYLE_POLICY_NOT_MARKET_TEMPERATURE'
    if is_retail_fuel_price_story(full_text):
        return None, 'RETAIL_FUEL_PRICE_NOT_MARKET_TEMPERATURE'
    if is_single_company_listing_story(full_text):
        return None, 'SINGLE_COMPANY_LISTING_NOT_MARKET_TEMPERATURE'
    if is_corporate_crime_nonmarket_story(full_text):
        return None, 'CORPORATE_CRIME_NOT_MARKET_TEMPERATURE'
    if is_political_context_nonmarket_story(full_text):
        return None, 'POLITICAL_CONTEXT_NOT_MARKET_TEMPERATURE'
    if is_science_tech_nonmarket_story(full_text):
        return None, 'SCIENCE_TECH_NOT_MARKET_TEMPERATURE'
    if is_low_impact_policy_noise(full_text):
        return None, 'LOW_IMPACT_POLICY_NOT_MARKET_TEMPERATURE'
    if is_local_semiconductor_policy_noise(full_text):
        return None, 'LOCAL_SEMICONDUCTOR_POLICY_NOT_MARKET_TEMPERATURE'
    if is_theme_or_opinion_noise(full_text):
        return None, 'OPINION_OR_THEME_NOT_DIRECT_MARKET_TEMPERATURE'
    if is_market_history_or_obituary(full_text):
        return None, 'MARKET_HISTORY_OR_OBITUARY_NOT_TODAY_TEMPERATURE'
    published_dt = parse_utc(published_at)
    if not published_dt:
        return None, 'MISSING_PUBLISHED_AT'
    age_hours = (datetime.now(timezone.utc) - published_dt).total_seconds() / 3600.0
    if age_hours > MAX_NEWS_AGE_HOURS:
        return None, 'STALE_OVER_24H'

    matched_rules = []
    for rule in MARKET_RELEVANCE_RULES:
        if rule_matches_headline(rule, headline, headline_lower):
            matched_rules.append(rule)
    if not matched_rules:
        return None, 'MARKET_IMPACT_LOW'
    if has_bellwether_company_context(full_text):
        matched_rules.sort(key=lambda rule: 0 if rule.get('category') == 'bellwether_company' else 1)


    is_critical, critical_reason = critical_market_event(headline)

    primary = matched_rules[0]
    tags: list[str] = []
    related_factors: list[str] = []
    for rule in matched_rules:
        for tag in rule['tags']:
            if tag not in tags:
                tags.append(tag)
        for factor in rule['relatedFactors']:
            if factor not in related_factors:
                related_factors.append(factor)
    target = target_from_headline(headline, primary['target'])
    impact = market_impact_components(headline, source_name, matched_rules, age_hours)
    if is_critical:
        impact['marketImpactScore'] = min(100.0, impact['marketImpactScore'] + 12.0)
    if impact['marketImpactScore'] < MARKET_IMPACT_THRESHOLD:
        return None, 'MARKET_IMPACT_SCORE_BELOW_THRESHOLD'

    quality_score = 0.62 + (impact['marketImpactScore'] / 100.0) * 0.28
    quality_score -= min(max(age_hours, 0) / 72.0, 0.20)
    if is_critical:
        quality_score += 0.10
    if source_name in {'연합뉴스', 'MBC 뉴스', 'YTN', 'Reuters', 'Yahoo Finance'}:
        quality_score += 0.05

    why = primary['why']
    if inflation_stress_signal(headline):
        why = '물가가 높으면 금리 인하가 늦어질 수 있습니다. 그래서 주식시장에는 부담입니다.'
    elif impact.get('hasBellwetherCompanyContext'):
        why = '지수 비중이 큰 대표기업 뉴스라 개별 종목 판단이 아니라 시장 온도 근거로 봅니다.'
    elif impact['singleBrandEvent'] and impact['hasListedCompanyContext']:
        why = '단일 브랜드 이슈라도 실적·수출·업종·주가 맥락이 확인되어 시장 온도 참고 뉴스로 분류했습니다.'
    return {
        'category': primary['category'],
        'categoryLabel': primary['label'],
        'impactTarget': target,
        'impactTone': market_burden_tone(headline, headline_tone(headline)),
        'tags': tags[:4] or ['뉴스'],
        'relatedFactors': related_factors or ['news'],
        'whyImportant': why,
        'scoreAnchor': 'market_impact_formula_v1',
        'qualityScore': round(max(0.0, min(1.0, quality_score)), 3),
        'marketImpactScore': impact['marketImpactScore'],
        'marketImpactComponents': impact,
        'critical': is_critical,
        'criticalReason': critical_reason,
        'priorityTier': 'CRITICAL' if is_critical else 'STANDARD',
    }, 'PASS'


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except Exception:
        return None


def fetch_feed(feed: dict[str, str], *, timeout: int, limit: int) -> dict[str, Any]:
    request = urllib.request.Request(feed['url'], headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_000)
        root = ET.fromstring(raw)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for node in root.findall('.//item'):
            title = clean_text(node.findtext('title'))
            link = clean_text(node.findtext('link'))
            published_at = parse_date(node.findtext('pubDate'))
            if feed['sourceId'].startswith('rss:google-news'):
                title, source_name = split_google_news_source(title, 'Google News RSS')
            else:
                source_name = feed.get('label', 'RSS').split('—')[0].strip()
            if not title or not link or looks_suspicious_headline(title):
                continue
            dedupe_key = title.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append({
                'headline': title,
                'sourceName': source_name,
                'publishedAt': published_at,
                'url': link,
                'sourceId': feed['sourceId'],
                'region': feed.get('region') or ('US' if 'us' in feed['sourceId'] else 'KR'),
                'provider': 'public-rss',
                'licenseNote': 'public RSS headline cache only; no body or image scraping',
            })
            if len(items) >= limit:
                break
        return {'sourceId': feed['sourceId'], 'label': feed.get('label'), 'region': feed.get('region'), 'status': 'ok' if items else 'empty', 'urlHost': urllib.parse.urlparse(feed['url']).netloc, 'items': items}
    except Exception as error:
        return {'sourceId': feed['sourceId'], 'label': feed.get('label'), 'region': feed.get('region'), 'status': 'error', 'urlHost': urllib.parse.urlparse(feed['url']).netloc, 'error': type(error).__name__, 'items': []}


def categorize(headline: str) -> tuple[str, str, list[str]]:
    text = headline.lower()
    tags: list[str] = []
    target = 'market'
    tone = 'neutral'
    if any(token in headline for token in ['코스피', '코스닥', '원/달러', '원달러', '환율', '삼성전자', '하이닉스']):
        target = 'kr'
    if any(token in text for token in ['s&p', 'nasdaq', 'qqq', 'spy', 'soxx', 'smh', 'fed', 'treasury']):
        target = 'us'
    if any(token in headline for token in ['반도체', 'AI']) or any(token in text for token in ['semiconductor', 'chip', 'ai']):
        tags.append('반도체')
    if any(token in headline for token in ['환율', '원달러', '달러']) or 'dollar' in text:
        tags.append('환율')
    if any(token in headline for token in ['금리', '연준']) or any(token in text for token in ['fed', 'yield', 'treasury', 'rate']):
        tags.append('금리')
    if any(token in headline for token in ['코스피', '코스닥', '나스닥', 'S&P']) or any(token in text for token in ['nasdaq', 's&p', 'market', 'stocks']):
        tags.append('지수')
    if any(token in headline for token in ['상승', '강세', '반등']) or any(token in text for token in ['rally', 'rise', 'gain', 'higher']):
        tone = 'positive'
    if any(token in headline for token in ['하락', '약세', '급락', '부담']) or any(token in text for token in ['fall', 'drop', 'lower', 'risk']):
        tone = 'negative'
    return target, tone, tags or ['뉴스']


def normalize_items(feed_results: list[dict[str, Any]], max_items: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    filtered_reasons: dict[str, int] = {}
    for result in feed_results:
        for item in result.get('items', []):
            headline = item['headline']
            key = news_dedupe_key(item)
            if key in seen:
                filtered_reasons['DUPLICATE'] = filtered_reasons.get('DUPLICATE', 0) + 1
                continue
            seen.add(key)
            relevance, reason = classify_relevance(headline, item.get('sourceName') or result.get('label') or 'RSS', item.get('publishedAt'))
            if not relevance:
                filtered_reasons[reason] = filtered_reasons.get(reason, 0) + 1
                continue
            translated_headline = koreanize_english_headline(headline)
            if has_korean(headline):
                display_headline = cause_aware_display_headline(headline, headline)
            else:
                display_headline = translated_headline
            if not has_korean(headline) and not translated_headline:
                filtered_reasons['UNTRANSLATED_ENGLISH_HEADLINE'] = filtered_reasons.get('UNTRANSLATED_ENGLISH_HEADLINE', 0) + 1
                continue
            translated_from_english = bool(translated_headline and not has_korean(headline))
            final_impact_tone = market_burden_tone(display_headline or headline, relevance['impactTone'])
            original_headline = headline if (translated_from_english or (display_headline and display_headline != headline)) else None
            out.append({
                'headline': headline,
                'displayHeadline': display_headline or headline,
                'originalHeadline': original_headline,
                'language': 'en' if translated_from_english else 'ko',
                'translationNote': '해외 원문 제목을 한국어로 옮긴 시장 온도 요약입니다.' if translated_from_english else None,
                'sourceName': item.get('sourceName') or result.get('label') or 'RSS',
                'publishedAt': item.get('publishedAt'),
                'url': item.get('url'),
                'impactTarget': relevance['impactTarget'],
                'impactTone': final_impact_tone,
                'category': relevance['category'],
                'categoryLabel': relevance['categoryLabel'],
                'tags': relevance['tags'],
                'relatedFactors': relevance['relatedFactors'],
                'whyImportant': relevance['whyImportant'],
                'scoreAnchor': relevance['scoreAnchor'],
                'qualityScore': relevance['qualityScore'],
                'marketImpactScore': relevance.get('marketImpactScore'),
                'marketImpactComponents': relevance.get('marketImpactComponents'),
                'critical': relevance.get('critical') is True,
                'criticalReason': relevance.get('criticalReason'),
                'priorityTier': relevance.get('priorityTier') or ('CRITICAL' if relevance.get('critical') else 'STANDARD'),
                'sourceId': item.get('sourceId') or result.get('sourceId'),
                'region': item.get('region') or result.get('region') or ('US' if relevance['impactTarget'] == 'us' else 'KR'),
                'provider': 'public-rss',
                'licenseNote': item.get('licenseNote') or 'public RSS headline cache only; no body or image scraping',
            })
    def sort_key(item: dict[str, Any]) -> tuple[int, float, float, int]:
        return (1 if item.get('critical') else 0, float(item.get('marketImpactScore') or 0), float(item.get('qualityScore') or 0), 1 if item.get('category') in {'market_event', 'macro', 'central_bank'} else 0)

    out = sorted(out, key=sort_key, reverse=True)
    out = issue_capped_items(out, per_issue_limit=3)
    overseas_items = [item for item in out if item.get('region') == 'US']
    us_items = [item for item in out if item.get('region') == 'US' or item.get('impactTarget') == 'us']
    kr_items = [item for item in out if item.get('region') == 'KR' or item.get('impactTarget') == 'kr']
    kr_target_items = [item for item in out if item.get('impactTarget') == 'kr']
    us_target_items = [item for item in out if item.get('impactTarget') == 'us']
    balanced: list[dict[str, Any]] = []
    seen_balanced: set[str] = set()
    target_overseas = min(len(overseas_items), max(3, max_items // 3)) if max_items >= 6 else min(len(overseas_items), 1)
    target_kr = min(len(kr_items), max(4, max_items // 3)) if max_items >= 10 else min(len(kr_items), 2)
    target_us = min(len(us_items), max(4, max_items // 3)) if max_items >= 10 else min(len(us_items), 2)
    critical_items = [item for item in out if item.get('critical')]
    def add_item(item: dict[str, Any]) -> bool:
        key = str(item.get('url') or item.get('headline')).lower()
        if key in seen_balanced:
            return False
        balanced.append(item)
        seen_balanced.add(key)
        return True
    # Critical events should lead. Translated overseas cards get a minimum
    # presence, but the quota must not override stronger market triggers.
    critical_lead_limit = min(len(critical_items), max(4, max_items // 4))
    for bucket, limit in [
        (critical_items, critical_lead_limit),
        (kr_items, target_kr),
        (us_items, target_us),
        (overseas_items, target_overseas),
        (out, max_items),
    ]:
        count = 0
        for item in bucket:
            if add_item(item):
                count += 1
            if len(balanced) >= max_items or count >= limit:
                break
        if len(balanced) >= max_items:
            break
    if balanced and not any(item.get('impactTarget') == 'kr' for item in balanced):
        for item in kr_target_items:
            key = str(item.get('url') or item.get('headline')).lower()
            if key not in seen_balanced:
                removed = balanced.pop()
                seen_balanced.discard(str(removed.get('url') or removed.get('headline')).lower())
                balanced.append(item)
                seen_balanced.add(key)
                break
    if balanced and not any(item.get('impactTarget') == 'us' for item in balanced):
        for item in us_target_items:
            key = str(item.get('url') or item.get('headline')).lower()
            if key not in seen_balanced:
                removed = balanced.pop()
                seen_balanced.discard(str(removed.get('url') or removed.get('headline')).lower())
                balanced.append(item)
                seen_balanced.add(key)
                break
    selected = sorted(balanced[:max_items], key=sort_key, reverse=True)
    return selected, {
        'filteredReasons': filtered_reasons,
        'filteredOutCount': sum(filtered_reasons.values()),
        'balancedPolicy': 'critical_first_then_translated_overseas_and_kr_mix',
        'criticalCount': sum(1 for item in selected if item.get('critical')),
        'translatedOverseasCount': sum(1 for item in selected if item.get('region') == 'US' and item.get('originalHeadline')),
        'issueClusterCount': len({item.get('issueClusterKey') for item in selected if item.get('issueClusterKey')}),
    }


def normalized_news_topic_text(item: dict[str, Any]) -> str:
    text = ' '.join(str(value or '') for value in [item.get('displayHeadline'), item.get('headline')]).lower()
    text = re.sub(r'["“”\'‘’….,!?|()\[\]{}<>:;·・~\-_/\\]', ' ', text)
    text = re.sub(r'\b(nyt|new york times|뉴욕타임스|美|미국|韓|한국)\b', ' ', text, flags=re.I)
    text = re.sub(r'삼전닉스', '삼성 하이닉스', text)
    text = re.sub(r'삼성전자|삼전|삼멘|삼맨', '삼성', text)
    text = re.sub(r'sk하이닉스|하이닉스|하멘|하맨', '하이닉스', text)
    return re.sub(r'\s+', ' ', text).strip()


def issue_cluster_key(item: dict[str, Any]) -> str:
    text = normalized_news_topic_text(item)
    raw = ' '.join(str(value or '') for value in [item.get('displayHeadline'), item.get('headline'), item.get('sourceName')])
    if re.search(r'(이란|중동|호르무즈|iran|hormuz|u\s*s\s*iran|us\s*iran)', raw, re.I) and re.search(r'(유가|원유|oil|wti|협상|negotiation|talks|strait|긴장|완화)', raw, re.I):
        return 'issue:iran_oil_risk'
    if re.search(r'(s&p|s\s*p\s*500|sp500|나스닥|nasdaq|다우|dow|wall street|미국\s*주요\s*지수|뉴욕증시)', raw, re.I) and re.search(r'(선물|futures|상승|하락|약세|강세|반등|rally|slip|edge|higher|lower|mixed|혼조|개장|premarket)', raw, re.I):
        return 'issue:us_index_market_wrap'
    if re.search(r'(sk\s*하이닉스|하이닉스|삼성전자|삼성)', raw, re.I) and re.search(r'(시총|1위|대장주|왕좌|반도체\s*랠리)', raw, re.I):
        return 'issue:kr_semiconductor_leadership'
    if re.search(r'(코스피|코스닥|한국\s*증시|국내\s*증시)', raw, re.I) and re.search(r'(마감|개장|수급|외국인|기관|환율|최고치|숨고르기|급등|급락|폭락|반등|회복|상승|하락|약세|강세|조정|\d[\d,]*선)', raw, re.I):
        return 'issue:kr_market_flow'
    stop_words = {'기사', '뉴스', '오늘', '관련', '소개한', '집중조명'}
    tokens = [token for token in text.split() if len(token) >= 2 and token not in stop_words]
    return 'issue:' + '|'.join(tokens[:5])


def news_dedupe_key(item: dict[str, Any]) -> str:
    text = normalized_news_topic_text(item)
    raw = ' '.join(str(value or '') for value in [item.get('displayHeadline'), item.get('headline')])
    if all(token in text for token in ['삼성', '하이닉스', '반도체']) and re.search(r'nyt|뉴욕타임스|열풍|소개|집중조명', raw, re.I):
        return 'topic:kr_semiconductor_nyt_slang'
    url = str(item.get('url') or '').strip().lower()
    if url:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        article_id = (params.get('url') or params.get('u') or [''])[0]
        if article_id:
            return f'url:{article_id.lower()}'
        if parsed.netloc and parsed.path and parsed.path != '/':
            return f"url:{parsed.netloc}{parsed.path.rstrip('/')}"
    stop_words = {'기사', '뉴스', '오늘', '관련', '소개한', '집중조명'}
    tokens: list[str] = []
    for token in text.split():
        if len(token) >= 2 and token not in stop_words and token not in tokens:
            tokens.append(token)
    return 'topic:' + '|'.join(tokens[:8])


def issue_capped_items(items: list[dict[str, Any]], per_issue_limit: int = 3) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    capped: list[dict[str, Any]] = []
    for item in items:
        key = issue_cluster_key(item)
        count = counts.get(key, 0)
        if count >= per_issue_limit:
            continue
        counts[key] = count + 1
        item['issueClusterKey'] = key
        capped.append(item)
    return capped


def build_report(timeout: int, per_feed_limit: int, max_items: int, ttl_minutes: int) -> dict[str, Any]:
    feed_results = [fetch_feed(feed, timeout=timeout, limit=per_feed_limit) for feed in DEFAULT_FEEDS]
    items, filter_summary = normalize_items(feed_results, max_items=max_items)
    generated_at = utc_now()
    return {
        'mode': 'public_rss_headline_cache',
        'generatedAt': generated_at,
        'ttlMinutes': ttl_minutes,
        'nextRefreshAt': utc_in(ttl_minutes),
        'recommendedSchedule': NEWS_RECOMMENDED_SCHEDULE,
        'status': 'ok' if items else ('partial' if any(result.get('status') == 'ok' for result in feed_results) else 'unavailable'),
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'bodyScrapingEnabled': False,
        'imageScrapingEnabled': False,
        'selectionPolicy': 'market_temperature_evidence_only',
        'maxAgeHours': MAX_NEWS_AGE_HOURS,
        **filter_summary,
        'feedResults': feed_results,
        'items': items,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument('--per-feed-limit', type=int, default=25)
    parser.add_argument('--max-items', type=int, default=30)
    parser.add_argument('--ttl-minutes', type=int, default=DEFAULT_NEWS_TTL_MINUTES)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    report = build_report(timeout=args.timeout, per_feed_limit=args.per_feed_limit, max_items=args.max_items, ttl_minutes=args.ttl_minutes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"PolarMeter news RSS probe: {report['status']} items={len(report['items'])} output={args.output}")
    return 0 if report['status'] in {'ok', 'partial', 'unavailable'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
