from pathlib import Path
import json
from polarmeter_news_rss_probe import headline_tone, market_burden_tone, issue_cluster_key, tone_aligned_why, normalize_items
from polarmeter_news_fact_semantics import inflation_release, mask_negated_moves
from datetime import datetime, timezone

cases = json.loads(Path(__file__).with_name('polarmeter_news_fact_cases.json').read_text())
checks = 0
for c in cases['tones']:
    assert headline_tone(c['headline']) == c['tone'], (c, headline_tone(c['headline']))
    assert market_burden_tone(c['headline']) == c['tone'], (c, market_burden_tone(c['headline']))
    checks += 2
now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
for c in cases['pairs']:
    a = issue_cluster_key({'headline': c['a'], 'publishedAt': now})
    b = issue_cluster_key({'headline': c['b'], 'publishedAt': now})
    if not c['same']:
        assert a != b, (c, a, b)
        checks += 1
for i, c in enumerate(cases['tones']):
    items, _ = normalize_items([{'label': 'QA', 'items': [{'headline': c['headline'], 'sourceName': 'QA', 'publishedAt': now, 'url': f'https://example.invalid/fact/{i}'}]}], 1)
    assert items and items[0]['impactTone'] == c['tone'], (c, items)
    release = inflation_release(c['headline'])
    if release and release['surprise'] == 'below' and c['tone'] == 'positive':
        assert '예상보다 낮아' in items[0]['whyImportant'], items[0]
    if mask_negated_moves(c['headline']) != c['headline']:
        assert '유가 하락' not in items[0]['whyImportant'], items[0]
    checks += 1
print(f'PASS server fact contract: {checks} checks')
