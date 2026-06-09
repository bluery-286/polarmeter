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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = WORKSPACE / 'testflight/news-rss-probe-latest.json'
DEFAULT_TIMEOUT = 8
USER_AGENT = 'PolarMeter-B1-NewsCache/0.1 (+https://bluery-286.github.io/polarmeter/support.html)'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def google_news_url(query: str) -> str:
    params = urllib.parse.urlencode({'q': query, 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'})
    return f'https://news.google.com/rss/search?{params}'


DEFAULT_FEEDS = [
    {
        'sourceId': 'rss:google-news:market-context-kr',
        'label': 'Google News RSS — 시장/환율/반도체',
        'url': google_news_url('코스피 OR 나스닥 OR S&P500 OR 원달러 OR 반도체 when:2d'),
    },
    {
        'sourceId': 'rss:yahoo-finance:us-market',
        'label': 'Yahoo Finance RSS — US market ETFs',
        'url': 'https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,SOXX,SMH&region=US&lang=en-US',
    },
]


def clean_text(value: str | None) -> str:
    text = re.sub(r'<[^>]+>', '', value or '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


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
    if any(token in title for token in ['8천피', '8천선', '8000선', '8,000선', '7% 넘게 급등']):
        return True
    # Avoid user-facing wording that our investment-action text guard flags.
    if any(token in title for token in ['매수', '매도', '추격매수']):
        return True
    return False


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
                'provider': 'public-rss',
                'licenseNote': 'public RSS headline cache only; no body or image scraping',
            })
            if len(items) >= limit:
                break
        return {'sourceId': feed['sourceId'], 'label': feed.get('label'), 'status': 'ok' if items else 'empty', 'urlHost': urllib.parse.urlparse(feed['url']).netloc, 'items': items}
    except Exception as error:
        return {'sourceId': feed['sourceId'], 'label': feed.get('label'), 'status': 'error', 'urlHost': urllib.parse.urlparse(feed['url']).netloc, 'error': type(error).__name__, 'items': []}


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


def normalize_items(feed_results: list[dict[str, Any]], max_items: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in feed_results:
        for item in result.get('items', []):
            headline = item['headline']
            key = headline.lower()
            if key in seen:
                continue
            seen.add(key)
            target, tone, tags = categorize(headline)
            out.append({
                'headline': headline,
                'sourceName': item.get('sourceName') or result.get('label') or 'RSS',
                'publishedAt': item.get('publishedAt'),
                'url': item.get('url'),
                'impactTarget': target,
                'impactTone': tone,
                'tags': tags,
                'sourceId': item.get('sourceId') or result.get('sourceId'),
                'provider': 'public-rss',
                'licenseNote': item.get('licenseNote') or 'public RSS headline cache only; no body or image scraping',
            })
            if len(out) >= max_items:
                return out
    return out


def build_report(timeout: int, per_feed_limit: int, max_items: int) -> dict[str, Any]:
    feed_results = [fetch_feed(feed, timeout=timeout, limit=per_feed_limit) for feed in DEFAULT_FEEDS]
    items = normalize_items(feed_results, max_items=max_items)
    return {
        'mode': 'public_rss_headline_cache',
        'generatedAt': utc_now(),
        'status': 'ok' if items else ('partial' if any(result.get('status') == 'ok' for result in feed_results) else 'unavailable'),
        'paidProviderEnabled': False,
        'clientDirectProviderCalls': False,
        'bodyScrapingEnabled': False,
        'imageScrapingEnabled': False,
        'feedResults': feed_results,
        'items': items,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument('--per-feed-limit', type=int, default=8)
    parser.add_argument('--max-items', type=int, default=6)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    report = build_report(timeout=args.timeout, per_feed_limit=args.per_feed_limit, max_items=args.max_items)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"PolarMeter news RSS probe: {report['status']} items={len(report['items'])} output={args.output}")
    return 0 if report['status'] in {'ok', 'partial', 'unavailable'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
