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

CIVIC_POLICY_MARKET_OVERRIDE_PATTERNS = [
    re.compile(r'(코스피|코스닥|나스닥|S&P\s*500|S&P500|SP500|VIX|증시|주가|상장|실적|매출|영업이익|가이던스|컨센서스)', re.I),
    re.compile(r'(소비\s*지수|소매\s*판매|내수|CPI|물가|인플레이션|유통업|숙박업|항공주|여행주|면세점|수혜\s*섹터)', re.I),
    re.compile(r'(산업용|기업|생산|제조업|수출|무역|관세|세제|법인세|자본이득세|공급망|물류|항만|에너지\s*가격)', re.I),
    re.compile(r'(외국인\s*(투자자|기관)|순매수|순매도|기관\s*(순매수|순매도))', re.I),
]


def is_civic_lifestyle_policy(text: str) -> bool:
    return any(pattern.search(text) for pattern in CIVIC_LIFESTYLE_POLICY_PATTERNS)


def has_civic_policy_market_override(text: str) -> bool:
    return any(pattern.search(text) for pattern in CIVIC_POLICY_MARKET_OVERRIDE_PATTERNS)

LISTED_COMPANY_MARKET_CONTEXT_PATTERNS = [
    re.compile(r'(실적|매출|영업이익|수출|가이던스|어닝|컨센서스|업종|섹터|주가|증시|코스피|코스닥|상승|하락|급등|급락)', re.I),
]

SINGLE_BRAND_EVENT_PATTERNS = [
    re.compile(r'(팝업스토어|팝업\s*스토어|가봤더니|팬덤|브랜드\s*캠페인|맛집|신제품|편의점|성수)', re.I),
]

def market_impact_components(headline: str, source_name: str, matched_rules: list[dict[str, Any]], age_hours: float) -> dict[str, Any]:
    text = f'{headline} {source_name}'
    lower = text.lower()
    has_market_linkage = any(pattern.search(text) for pattern in MARKET_LINKAGE_PATTERNS)
    has_magnitude = any(pattern.search(text) for pattern in MAGNITUDE_PATTERNS)
    has_listed_company_context = any(pattern.search(text) for pattern in LISTED_COMPANY_MARKET_CONTEXT_PATTERNS)
    single_brand_event = any(pattern.search(text) for pattern in SINGLE_BRAND_EVENT_PATTERNS)
    civic_lifestyle_policy = is_civic_lifestyle_policy(text)
    civic_market_override = has_civic_policy_market_override(text)
    categories = {rule['category'] for rule in matched_rules}

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
    else:
        market_linkage = 55 if has_market_linkage else 20
        breadth = 40

    if civic_lifestyle_policy and not civic_market_override:
        breadth = min(breadth, 10)
        market_linkage = min(market_linkage, 16)
    elif civic_lifestyle_policy and civic_market_override:
        breadth = min(breadth, 62)
        market_linkage = min(max(market_linkage, 58), 70)

    if single_brand_event and not has_listed_company_context:
        breadth = min(breadth, 12)
        market_linkage = min(market_linkage, 18)
    elif has_listed_company_context and single_brand_event:
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
        'singleBrandEvent': single_brand_event,
        'civicLifestylePolicy': civic_lifestyle_policy,
        'civicPolicyMarketOverride': civic_market_override,
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
    (re.compile(r'buy\s+everything\s+ai.*aiq', re.I), 'AIQ ETF로 본 AI 투자 열풍 점검'),
    (re.compile(r'energy\s+stocks\s+are\s+back.*iye', re.I), '에너지주 반등에 IYE ETF 강세'),
    (re.compile(r'tech\s+frenzy.*mgk\s+dip', re.I), '대형 기술주 조정 매수세가 이어진다는 분석'),
    (re.compile(r'equity\s+futures\s+higher.*us\s+attacks\s+on\s+iran', re.I), '이란 관련 긴장 속 미국 주가지수 선물 상승'),
    (re.compile(r'tech\s+rebound\s+lifts\s+wall\s+street.*asia.*europe', re.I), '기술주 반등에 미국 증시 선물 강세, 아시아 혼조·유럽 상승'),
    (re.compile(r's&p\s*500.*nasdaq.*dow.*end\s+higher.*trump\s+signals\s+iran\s+deal', re.I), '트럼프가 이란 합의를 시사하자 S&P500·나스닥·다우 상승 마감'),
    (re.compile(r'us\s+stock\s+market\s+today.*dow\s+jumps.*s&p\s*500.*nasdaq\s+rebound', re.I), '다우 상승, S&P500·나스닥 반등 — 반도체주 회복세'),
    (re.compile(r'iran\s+war\s+gets\s+hot\s+again.*trump.*iran.*oil', re.I), '이란 전쟁 긴장 재고조와 원유 리스크 점검'),
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
        'why': '중앙은행·금리 신호는 성장주와 환율, 위험 선호를 동시에 흔드는 핵심 변수입니다.',
    },
    {
        'category': 'macro',
        'label': '매크로',
        'keywords': ['cpi', 'pce', '고용', '실업', 'gdp', 'pmi', '환율', '원달러', '원/달러', '달러', 'dollar', '유가', 'wti', 'oil'],
        'tags': ['매크로'],
        'target': 'bridge',
        'relatedFactors': ['macro'],
        'why': '환율·물가·고용·유가 같은 매크로 변수는 시장 온도의 배경 압력입니다.',
    },
    {
        'category': 'market_event',
        'label': '시장이벤트',
        'keywords': ['코스피', 'kospi', '코스닥', '나스닥', 's&p', 'sp500', 's&p500', 'vix', '급락', '폭락', '급등', '반등', '상승', '하락', '외국인', '기관', '순매수', '순매도', 'sidecar', '서킷브레이커'],
        'tags': ['지수'],
        'target': 'market',
        'relatedFactors': ['indices', 'news'],
        'why': '지수 급변과 수급 변화는 오늘 시장 온도를 설명하는 직접 트리거입니다.',
    },
    {
        'category': 'semiconductor_bridge',
        'label': '반도체',
        'keywords': ['반도체', 'hbm', 'ai', 'soxx', 'smh', '엔비디아', 'nvidia', '삼성전자', '하이닉스'],
        'tags': ['반도체'],
        'target': 'bridge',
        'relatedFactors': ['indices', 'news'],
        'why': '반도체 이슈는 미국 기술주와 한국 증시를 이어주는 브릿지 신호입니다.',
    },
    {
        'category': 'geopolitics_supply',
        'label': '정책·공급망',
        'keywords': ['관세', '수출규제', '중동', '이란', '휴전', '종전', '합의', '전쟁', '제재', '공급망', '에너지', '원유', 'iran', 'ceasefire', 'truce', 'war risk', 'airstrike', 'escalation'],
        'tags': ['정책', '공급망'],
        'target': 'global',
        'relatedFactors': ['macro', 'news'],
        'why': '정책·지정학·공급망 충격은 위험 선호와 비용 부담을 바꾸는 외부 변수입니다.',
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
        'sourceId': 'rss:google-news:market-context-us-major',
        'label': 'Google News RSS — US market translated',
        'region': 'US',
        'url': google_news_url('S&P 500 Nasdaq Dow futures Fed Treasury yield Wall Street stocks when:1d -buy -dividend -portfolio', hl='en-US', gl='US', ceid='US:en'),
    },
    {
        'sourceId': 'rss:google-news:market-context-us-macro',
        'label': 'Google News RSS — global macro translated',
        'region': 'US',
        'url': google_news_url('Fed FOMC Treasury yield dollar index VIX oil Iran Middle East markets when:1d -buy -dividend -portfolio', hl='en-US', gl='US', ceid='US:en'),
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
    return text


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
    has_futures = 'future' in lower
    has_fed = re.search(r'\bfed\b|fomc|rate decision|treasury|yield', lower) is not None
    has_dollar = re.search(r'dollar|eur/usd|sterling|pound', lower) is not None
    has_inflation = re.search(r'\bpce\b|\bcpi\b|inflation', lower) is not None
    has_oil_geo = re.search(r'iran|hormuz|middle east|ceasefire|trump.*iran|us-iran|oil|crude', lower) is not None
    has_chip = re.search(r'nvidia|chipmaker|semiconductor|micron|broadcom|ai stock|ai stocks', lower) is not None
    positive = re.search(r'edge higher|higher|climb|rise|rally|rebound|advance|jump|surge|gain', lower) is not None
    negative = re.search(r'slip|drop|fall|lower|sell-off|selloff|plunge|slump|risk-off|lost|decline', lower) is not None

    if 'eur/usd weekly outlook' in lower and 'fomc' in lower:
        return 'FOMC 이후 달러 강세가 이어질지 점검하는 해외 외환시장 전망'
    if re.search(r'gold|silver', lower) and negative and has_fed:
        return '연준 신호에 금·은 가격 약세, 안전자산 수요를 함께 점검'
    if has_futures and (has_sp500 or has_nasdaq or has_dow) and positive and has_fed:
        return '연준 금리 결정을 앞두고 미국 주요 지수 선물이 소폭 상승'
    if has_futures and (has_sp500 or has_nasdaq or has_dow) and negative:
        return '미국 주요 지수 선물이 약세를 보이며 개장 전 부담을 확인'
    if has_futures and has_oil_geo:
        return '이란·중동 이슈 속 미국 지수 선물과 유가 리스크 점검'
    if has_oil_geo and re.search(r'oil|crude|hormuz', lower):
        return '이란·중동 긴장으로 유가와 시장 위험선호를 함께 점검'
    if has_oil_geo:
        return '이란·중동 이슈가 글로벌 위험선호에 미치는 영향 점검'
    if has_inflation:
        return '미국 물가 지표가 주식·달러·금 가격에 미칠 영향 점검'
    if has_dollar and has_fed:
        return '연준·금리 신호 이후 달러와 환율 흐름 점검'
    if has_fed:
        return '연준·미국 금리 흐름이 증시에 주는 부담 확인'
    if has_chip and negative:
        return '반도체·AI주 약세가 미국 기술주 흐름에 주는 부담 확인'
    if has_chip and positive:
        return '반도체·AI주 반등이 미국 기술주 흐름을 지지하는지 점검'
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
        if positive:
            return f'{subject} 상승 흐름이 미국장 온도에 미치는 영향 점검'
        if negative:
            return f'{subject} 약세 흐름이 미국장 부담으로 이어지는지 점검'
        return f'{subject} 흐름을 해외 원문 기준으로 점검'
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
    if any(token in title for token in ['8천피', '8천선', '8000선', '8,000선', '7% 넘게 급등']):
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
    if any(token in headline for token in ['종전', '휴전', '합의', '환호']) or any(token in text for token in ['ceasefire', 'truce', 'deal coming soon', 'deal signed']):
        return 'positive'
    if any(token in headline for token in ['급등락', '널뛰기', '현기증', '공포', '투매', '하락', '약세', '급락', '폭락', '부담']) or any(token in text for token in ['fall', 'drop', 'lower', 'risk', 'selloff', 'volatility']):
        return 'negative'
    if any(token in headline for token in ['상승', '강세', '반등', '급등']) or any(token in text for token in ['rally', 'rise', 'gain', 'higher', 'rebound']):
        return 'positive'
    return 'neutral'


def target_from_headline(headline: str, default_target: str) -> str:
    text = headline.lower()
    if any(token in headline for token in ['코스피', '코스닥', '원/달러', '원·달러', '원달러', '환율', '외국인', '기관', '삼성전자', '하이닉스', '韓반도체']):
        return 'kr'
    if any(token in text for token in ['s&p', 'sp500', 'nasdaq', 'qqq', 'spy', 'fed', 'fomc', 'treasury', 'dow']) or any(token in headline for token in ['나스닥', '뉴욕증시']):
        return 'us'
    return default_target


def classify_relevance(headline: str, source_name: str, published_at: str | None) -> tuple[dict[str, Any] | None, str]:
    source_lower = source_name.lower()
    headline_lower = headline.lower()
    if any(hint.lower() in source_lower for hint in EXCLUDED_SOURCE_HINTS):
        return None, 'SOURCE_LOW_RELEVANCE'
    if any(hint.lower() in headline_lower for hint in EXCLUDED_HEADLINE_HINTS):
        return None, 'INVESTMENT_ACTION_OR_SINGLE_STOCK_NOISE'
    if ('?' in headline or 'should you' in headline_lower) and any(token in headline_lower for token in ['buy', 'stocks instead', 'smarter']):
        return None, 'INVESTMENT_ACTION_OR_SINGLE_STOCK_NOISE'
    if any(token in headline_lower for token in ['tax-free', 'cap gains', 'capital gains', 'portfolio']) and not any(token in headline_lower for token in ['s&p', 'nasdaq', 'vix', 'fed', 'fomc']):
        return None, 'PERSONAL_FINANCE_NOT_MARKET_TEMPERATURE'
    if '반도체' in headline and any(hint in headline_lower for hint in ['집값', '부동산', '아파트', 'gtx']):
        return None, 'REAL_ESTATE_NOT_MARKET_TEMPERATURE'
    if is_civic_lifestyle_policy(headline) and not has_civic_policy_market_override(headline):
        return None, 'CIVIC_LIFESTYLE_POLICY_NOT_MARKET_TEMPERATURE'
    published_dt = parse_utc(published_at)
    if not published_dt:
        return None, 'MISSING_PUBLISHED_AT'
    age_hours = (datetime.now(timezone.utc) - published_dt).total_seconds() / 3600.0
    if age_hours > MAX_NEWS_AGE_HOURS:
        return None, 'STALE_OVER_24H'

    matched_rules = []
    for rule in MARKET_RELEVANCE_RULES:
        if any(keyword.lower() in headline_lower for keyword in rule['keywords']):
            matched_rules.append(rule)
    if not matched_rules:
        return None, 'MARKET_IMPACT_LOW'


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
    if impact['singleBrandEvent'] and impact['hasListedCompanyContext']:
        why = '단일 브랜드 이슈라도 실적·수출·업종·주가 맥락이 확인되어 시장 온도 참고 뉴스로 분류했습니다.'
    return {
        'category': primary['category'],
        'categoryLabel': primary['label'],
        'impactTarget': target,
        'impactTone': headline_tone(headline),
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
            display_headline = koreanize_english_headline(headline)
            if not has_korean(headline) and not display_headline:
                filtered_reasons['UNTRANSLATED_ENGLISH_HEADLINE'] = filtered_reasons.get('UNTRANSLATED_ENGLISH_HEADLINE', 0) + 1
                continue
            translated_from_english = bool(display_headline and not has_korean(headline))
            out.append({
                'headline': headline,
                'displayHeadline': display_headline or headline,
                'originalHeadline': headline if display_headline else None,
                'language': 'en' if translated_from_english else 'ko',
                'translationNote': '해외 원문 제목을 한국어로 옮긴 시장 온도 요약입니다.' if translated_from_english else None,
                'sourceName': item.get('sourceName') or result.get('label') or 'RSS',
                'publishedAt': item.get('publishedAt'),
                'url': item.get('url'),
                'impactTarget': relevance['impactTarget'],
                'impactTone': relevance['impactTone'],
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
    overseas_items = [item for item in out if item.get('region') == 'US']
    us_items = [item for item in out if item.get('region') == 'US' or item.get('impactTarget') == 'us']
    kr_items = [item for item in out if item.get('region') == 'KR' or item.get('impactTarget') == 'kr']
    kr_target_items = [item for item in out if item.get('impactTarget') == 'kr']
    us_target_items = [item for item in out if item.get('impactTarget') == 'us']
    balanced: list[dict[str, Any]] = []
    seen_balanced: set[str] = set()
    target_overseas = min(len(overseas_items), max(2, max_items // 3)) if max_items >= 6 else min(len(overseas_items), 1)
    target_us = max(1, max_items // 2)
    target_kr = max(1, max_items - target_us)
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
    critical_lead_limit = min(len(critical_items), max(3, max_items // 3))
    for bucket, limit in [
        (critical_items, critical_lead_limit),
        (overseas_items, target_overseas),
        (kr_items, target_kr),
        (us_items, target_us),
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
    }


def normalized_news_topic_text(item: dict[str, Any]) -> str:
    text = ' '.join(str(value or '') for value in [item.get('displayHeadline'), item.get('headline')]).lower()
    text = re.sub(r'["“”\'‘’….,!?|()\[\]{}<>:;·・~\-_/\\]', ' ', text)
    text = re.sub(r'\b(nyt|new york times|뉴욕타임스|美|미국|韓|한국)\b', ' ', text, flags=re.I)
    text = re.sub(r'삼전닉스', '삼성 하이닉스', text)
    text = re.sub(r'삼성전자|삼전|삼멘|삼맨', '삼성', text)
    text = re.sub(r'sk하이닉스|하이닉스|하멘|하맨', '하이닉스', text)
    return re.sub(r'\s+', ' ', text).strip()


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
    parser.add_argument('--per-feed-limit', type=int, default=12)
    parser.add_argument('--max-items', type=int, default=10)
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
