"""Offline safety contract for conservative RU-UA ceasefire clustering."""
from __future__ import annotations

from datetime import datetime, timezone

from polarmeter_cache_snapshot import cached_news
from polarmeter_free_cache_worker import sanitize_public_news
from polarmeter_news_rss_probe import issue_capped_items, issue_cluster_key, normalize_items, russia_ukraine_ceasefire_issue_key


def item(headline: str, published_at: str, *, critical: bool = False, url: str | None = None) -> dict:
    return {
        'headline': headline,
        'displayHeadline': headline,
        'originalHeadline': headline,
        'publishedAt': published_at,
        'sourceName': 'Regression fixture',
        'url': url or f'https://example.invalid/news/{published_at.replace(":", "-")}',
        'critical': critical,
    }


def main() -> None:
    same_agreement = [
        item('러시아와 우크라이나는 멜라니아 트럼프 미국 영부인이 시작한 부분 휴전을 확인했습니다.', '2026-10-24T01:00:00Z', critical=True),
        item('러시아와 우크라이나, 멜라니아 트럼프 여사 덕분에 국지적 휴전 합의', '2026-10-24T01:01:00Z'),
    ]
    keys = {russia_ukraine_ceasefire_issue_key(row) for row in same_agreement}
    assert len(keys) == 1 and None not in keys
    assert issue_cluster_key(item('러·우, 국지적 휴전 합의', '2026-10-24T01:02:00Z')) == next(iter(keys))
    assert russia_ukraine_ceasefire_issue_key(item('달러·우 휴전 합의', '2026-10-24T01:03:00Z')) is None

    must_remain_separate = [
        '러시아와 우크라이나는 부분 휴전 합의를 부인했습니다.',
        '러시아와 우크라이나, 부분 휴전 제안 거절',
        '러시아와 우크라이나는 휴전에 합의하지 않았습니다.',
        '러시아와 우크라이나, 휴전 합의 아니다',
        '러시아와 우크라이나, 휴전 합의 가능성',
        '러시아와 우크라이나, 휴전 합의 기대',
        '러시아와 우크라이나, 휴전 합의 전망',
        '러시아와 우크라이나, 휴전 합의할까',
        '러시아와 우크라이나, 국지적 휴전 합의…11월 평화 협상 준비',
        '멜라니아 중재로 러·우 부분 휴전…고립 주민 구호·대피길 열린다',
        '러시아와 우크라이나, 부분 휴전 24일 발효 합의 확인',
        '러시아와 우크라이나, 24시간 부분 휴전 합의 확인',
        '러시아와 우크라이나, 월요일 부분 휴전 합의 확인',
        'Russia and Ukraine confirm ceasefire agreement on Monday',
        '이스라엘과 하마스, 가자지구 휴전 합의',
        '러시아와 우크라이나, 올레시키에서 별도 휴전 합의',
        '러시아와 우크라이나, 올레시키·헤르손에서 휴전 합의 확인',
    ]
    for headline in must_remain_separate:
        assert russia_ukraine_ceasefire_issue_key(item(headline, '2026-10-24T02:00:00Z')) is None, headline
    not_reached = item('Russia and Ukraine ceasefire agreement not reached', '2026-10-24T02:00:01Z')
    assert russia_ukraine_ceasefire_issue_key(not_reached) is None
    assert issue_cluster_key(not_reached).startswith('issue:related:ru_ua_ceasefire:update:')

    oleshky_titles = [
        '러시아와 우크라이나, 올레시키에서 부분 휴전 합의 확인',
        '러시아와 우크라이나, 올레슈키에서 국지적 휴전 합의',
        'Russia and Ukraine confirm local ceasefire agreement in Oleshky',
    ]
    oleshky_keys = {russia_ukraine_ceasefire_issue_key(item(title, '2026-10-24T02:01:00Z')) for title in oleshky_titles}
    assert len(oleshky_keys) == 1 and None not in oleshky_keys
    kherson = russia_ukraine_ceasefire_issue_key(item('러시아와 우크라이나, 헤르손에서 국지적 휴전 합의 확인', '2026-10-24T02:02:00Z'))
    assert kherson and kherson != next(iter(oleshky_keys))

    protected_followups = [
        'Russia and Ukraine confirm ceasefire agreement starting October 29',
        'Russia and Ukraine confirm ceasefire agreement starting October 30',
        'Russia and Ukraine confirm ceasefire agreement starting November 1',
        'Russia and Ukraine confirm ceasefire agreement starting November 2',
        'Russia and Ukraine ceasefire agreement opens humanitarian aid corridor in Kherson',
        'Russia and Ukraine ceasefire agreement allows evacuation route in Kherson',
        'Russia and Ukraine ceasefire agreement aid convoy reaches Kherson',
        'Russia and Ukraine ceasefire agreement takes effect after six hours in Kherson',
    ]
    protected_rows = [
        item(title, f'2026-10-24T02:{index:02d}:00Z', url=f'https://example.com/protected-{index}')
        for index, title in enumerate(protected_followups)
    ]
    protected_capped = issue_capped_items(protected_rows, per_issue_limit=3)
    assert len(protected_capped) == len(protected_followups)
    protected_public = sanitize_public_news(cached_news({'status': 'ok', 'items': protected_capped}))
    assert len(protected_public['items']) == len(protected_followups)
    assert {row['headline'] for row in protected_public['items']} == set(protected_followups)

    additional_facts = [
        'Russia and Ukraine confirm ceasefire agreement, oil prices fall',
        'Russia and Ukraine confirm ceasefire agreement, stock markets rise',
        'Russia and Ukraine confirm ceasefire agreement, Putin resigns',
        'Oil prices fall as Russia and Ukraine confirm ceasefire agreement',
        '러시아와 우크라이나, 국지적 휴전 합의…국제유가 하락',
        '러시아와 우크라이나, 국지적 휴전 합의, 금융시장 반등',
    ]
    plain_agreement = item('Russia and Ukraine confirm ceasefire agreement', '2026-10-24T03:00:00Z')
    extra_rows = [
        item(title, f'2026-10-24T02:{index:02d}:00Z', url=f'https://example.com/additional-{index}')
        for index, title in enumerate(additional_facts)
    ]
    assert all(russia_ukraine_ceasefire_issue_key(row) is None for row in extra_rows)
    extra_public = sanitize_public_news(cached_news({'status': 'ok', 'items': issue_capped_items(extra_rows + [plain_agreement])}))
    assert len(extra_public['items']) == len(extra_rows) + 1
    assert set(additional_facts) <= {row['headline'] for row in extra_public['items']}

    rows = [
        same_agreement[0],
        same_agreement[1],
        item('러·우, 국지적 휴전 합의 확인', '2026-10-24T01:02:00Z'),
        item('Russia and Ukraine confirm local ceasefire agreement', '2026-10-24T01:03:00Z'),
    ]
    capped = issue_capped_items(rows, per_issue_limit=3)
    assert len(capped) == 1 and capped[0]['critical'] is True
    assert capped[0]['publishedAt'] == '2026-10-24T01:00:00Z', 'a later standard card must not replace the critical representative'
    assert capped[0]['relatedReportCount'] == len(rows)
    assert len(capped[0]['relatedReports']) == len(rows)
    assert all(set(report) <= {'headline', 'originalHeadline', 'sourceName', 'publishedAt', 'url'} for report in capped[0]['relatedReports'])
    assert issue_capped_items(rows, per_issue_limit=0) == []

    source_item = dict(capped[0])
    source_item['privateSecretMarker'] = 'DO_NOT_PUBLISH'
    source_item['relatedReports'] = [
        dict(capped[0]['relatedReports'][0], privateSecretMarker='DO_NOT_PUBLISH'),
        {'headline': 'unsafe credential URL', 'url': 'https://user:password@example.com/story'},
        {'headline': 'unsafe token URL', 'url': 'https://example.com/story?access_token=secret'},
        {'headline': 'unsafe key URL', 'url': 'https://example.com/story?key=secret'},
        {'headline': 'unsafe fragment token URL', 'url': 'https://example.com/story#access_token=secret'},
        {'headline': 'unsafe private address', 'url': 'https://127.0.0.1/story'},
        {'headline': 'unsafe scheme', 'url': 'file:///etc/passwd'},
        {'headline': 'unsafe non-string', 'url': ['https://example.com/story']},
        {'headline': 'bounded scalar fields', 'sourceName': ['not a string'], 'publishedAt': 123,
         'url': 'https://example.com/valid-story'},
    ]
    snapshot_news = cached_news({'status': 'ok', 'items': [source_item]})
    snapshot_item = snapshot_news['items'][0]
    assert snapshot_item['relatedReportCount'] == len(snapshot_item['relatedReports']) == 2
    assert set(snapshot_item['relatedReports'][0]) == {'headline', 'originalHeadline', 'sourceName', 'publishedAt', 'url'}
    assert set(snapshot_item['relatedReports'][1]) == {'headline', 'url'}
    assert snapshot_item['issueGrouping'] == 'related_topic_not_confirmed_same_event'
    assert 'privateSecretMarker' not in snapshot_item

    public_news = sanitize_public_news({'privateSecretMarker': 'DO_NOT_PUBLISH', 'items': [snapshot_item]})
    public_item = public_news['items'][0]
    assert public_item['relatedReportCount'] == len(public_item['relatedReports']) == 2
    assert set(public_item['relatedReports'][0]) == {'headline', 'originalHeadline', 'sourceName', 'publishedAt', 'url'}
    assert set(public_item['relatedReports'][1]) == {'headline', 'url'}
    assert public_item['relatedReports'][0]['url'] in {row['url'] for row in rows}
    assert public_item['issueGrouping'] == 'related_topic_not_confirmed_same_event'
    assert 'privateSecretMarker' not in public_news and 'privateSecretMarker' not in public_item['relatedReports'][0]

    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    end_to_end_titles = [
        same_agreement[0]['headline'],
        same_agreement[1]['headline'],
        '러시아와 우크라이나, 29일부터 헤르손주 지역 휴전 합의',
        '러시아와 우크라이나, 국지적 휴전 합의 가능성',
        '러시아와 우크라이나, 국지적 휴전 합의 부인',
    ]
    feed = {'sourceId': 'fixture-ru-ua', 'label': 'Regression fixture', 'region': 'US', 'items': [
        {
            'headline': headline,
            'sourceName': 'Regression fixture',
            'publishedAt': now,
            'url': f'https://example.com/e2e-{index}',
            'region': 'US',
        }
        for index, headline in enumerate(end_to_end_titles)
    ]}
    normalized, _ = normalize_items([feed], max_items=30)
    assert len(normalized) == 4, normalized
    follow_up_titles = {
        '러시아와 우크라이나, 29일부터 헤르손주 지역 휴전 합의',
        '러시아와 우크라이나, 국지적 휴전 합의 가능성',
        '러시아와 우크라이나, 국지적 휴전 합의 부인',
    }
    normalized_titles = {row.get('originalHeadline') or row.get('headline') for row in normalized}
    assert follow_up_titles <= normalized_titles, normalized_titles
    e2e_snapshot = cached_news({'status': 'ok', 'items': normalized})
    e2e_public = sanitize_public_news(e2e_snapshot)
    assert len(e2e_public['items']) == 4, e2e_public['items']
    public_titles = {row.get('originalHeadline') or row.get('headline') for row in e2e_public['items']}
    assert follow_up_titles <= public_titles, public_titles
    e2e_group = next(row for row in e2e_public['items'] if row.get('relatedReports'))
    assert e2e_group['relatedReportCount'] == len(e2e_group['relatedReports']) == 2
    assert all(set(report) <= {'headline', 'originalHeadline', 'sourceName', 'publishedAt', 'url'} for report in e2e_group['relatedReports'])
    assert all({'headline', 'url'} <= set(report) for report in e2e_group['relatedReports'])
    print('PASS conservative RU-UA issue grouping and two-stage public related-report preservation')


if __name__ == '__main__':
    main()
