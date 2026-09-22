"""Bounded RSS diagnostics for CI logs only; never copy headlines or URLs."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from polarmeter_news_rss_probe import DEFAULT_FEEDS

SOURCE_IDS = {feed['sourceId'] for feed in DEFAULT_FEEDS}
STATUSES = {'ok', 'empty', 'error', 'invalid_url'}
ERRORS = {'HTTPError', 'URLError', 'TimeoutError', 'ParseError', 'ConnectionResetError'}
FILTERS = {
    'DUPLICATE', 'DUPLICATE_DISPLAY_HEADLINE', 'TONE_EXPLANATION_CONFLICT',
    'UNSAFE_OR_INVALID_URL', 'UNTRANSLATED_ENGLISH_HEADLINE',
    'MISSING_PUBLISHED_AT', 'EXPIRED_MACRO_EVENT_PREVIEW',
    'MARKET_IMPACT_LOW', 'MARKET_IMPACT_SCORE_BELOW_THRESHOLD',
    'SOURCE_LOW_RELEVANCE', 'SOURCE_HEADLINE_NOT_MARKET_TEMPERATURE',
    'LOW_INFORMATION_QUOTE_HEADLINE',
    'LOCAL_UTILITY_NOT_MARKET_TEMPERATURE',
    'COMPANY_ENFORCEMENT_NO_BROAD_MARKET_LINK',
}
TOPICS = {
    'rates_central_banks': r'금리|연준|한국은행|\bfed\b|\bfomc\b|treasury|yield',
    'inflation': r'물가|\bcpi\b|\bpce\b|\bppi\b|inflation',
    'employment': r'고용|실업|payroll|unemployment|jobs report',
    'fx': r'환율|원.?달러|dollar|currency|forex',
    'energy_supply': r'유가|원유|송유관|정유|해협|\boil\b|pipeline|refinery|strait',
    'policy_geopolitics': r'관세|제재|전쟁|휴전|수출통제|tariff|sanction|ceasefire|export control',
    'indices': r'코스피|코스닥|나스닥|증시|서킷브레이커|nasdaq|dow|s&p|stock market',
}


def topic_coverage(report: dict) -> dict:
    """A review signal, not proof that no important event exists or is missing."""
    raw = [item for feed in report.get('feedResults', []) if isinstance(feed, dict)
           for item in (feed.get('items') if isinstance(feed.get('items'), list) else [])
           if isinstance(item, dict)] if isinstance(report.get('feedResults'), list) else []
    selected = report.get('items') if isinstance(report.get('items'), list) else []
    def matches(item, pattern):
        if not isinstance(item, dict):
            return False
        text = ' '.join(value for key in ('headline', 'originalHeadline')
                        if isinstance(value := item.get(key), str))
        return bool(re.search(pattern, text, re.I))
    out = {}
    now = datetime.now(timezone.utc)
    for topic, pattern in TOPICS.items():
        received = sum(matches(item, pattern) for item in raw)
        chosen = [item for item in selected if matches(item, pattern)]
        ages = []
        for item in chosen:
            try:
                timestamp = datetime.fromisoformat(item['publishedAt'].replace('Z', '+00:00'))
                if timestamp.tzinfo is not None:
                    age = int((now - timestamp).total_seconds() / 60)
                    if age >= 0:
                        ages.append(age)
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                continue
        out[topic] = {
            'receivedCount': received,
            'selectedCount': len(chosen),
            'reviewSelectionGap': received > 0 and not chosen,
            'newestSelectedAgeMinutes': min(ages) if ages else None,
        }
    return out


def safe_count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def summarize_news_sources(report: object) -> dict:
    if not isinstance(report, dict):
        return {'reportStatus': 'invalid'}
    results = report.get('feedResults')
    rows = []
    seen = set()
    if isinstance(results, list):
        for result in results[:256]:
            if not isinstance(result, dict):
                continue
            source = result.get('sourceId')
            if not isinstance(source, str) or source not in SOURCE_IDS or source in seen:
                continue
            seen.add(source)
            status, error = result.get('status'), result.get('error')
            first_error = result.get('firstErrorType')
            items = result.get('items')
            rows.append({
                'sourceId': source,
                'status': status if isinstance(status, str) and status in STATUSES else 'unknown',
                'errorType': error if isinstance(error, str) and error in ERRORS else ('other' if error else None),
                'receivedCount': len(items) if isinstance(items, list) else None,
                'attemptCount': safe_count(result.get('attemptCount')),
                'firstErrorType': first_error if isinstance(first_error, str) and first_error in ERRORS else ('other' if first_error else None),
            })
    reasons = report.get('filteredReasons')
    filters, other_count = {}, 0
    if isinstance(reasons, dict):
        for reason, value in reasons.items():
            count = safe_count(value)
            if count is None:
                continue
            if reason in FILTERS:
                filters[reason] = count
            else:
                other_count = min(1_000_000, other_count + count)
    items = report.get('items')
    return {
        'reportStatus': 'available',
        'sources': rows,
        'missingSourceCount': len(SOURCE_IDS - seen),
        'receivedCount': sum(row['receivedCount'] or 0 for row in rows),
        'selectedCount': len(items) if isinstance(items, list) else None,
        'filteredOutCount': safe_count(report.get('filteredOutCount')),
        'filteredReasons': filters,
        'otherFilteredCount': other_count,
        'topicCoverage': topic_coverage(report),
    }


def emit_news_source_diagnostics(path: Path) -> None:
    try:
        summary = summarize_news_sources(json.loads(path.read_text(encoding='utf-8')))
    except Exception:
        # Do not echo exception text, file paths, or malformed report contents.
        summary = {'reportStatus': 'unreadable'}
    print(json.dumps({'newsSourceDiagnostics': summary}, ensure_ascii=False), file=sys.stderr)
