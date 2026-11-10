"""Source-only facts: denied changes, inflation surprises and release identities."""
from __future__ import annotations
import re


def mask_negated_moves(text: str) -> str:
    text = re.sub(r'(?:완화|해소|휴전|종전|협상|합의|인하)(?:\s*(?:가능성|기대|계획|안))?\s*(?:를?\s*)?(?:배제|거부|불발|무산|실패|없|아니|하지\s*않|되지\s*않)', '부정된변화', text, flags=re.I)
    text = re.sub(r'\b(?:no|not|without)\s+(?:any\s+)?(?:easing|relief|ceasefire|deal|rate\s+cuts?)\b', 'denied move', text, flags=re.I)
    return re.sub(r'\b(?:rules?\s+out|rejects?|denies?)\s+(?:sanctions?\s+)?(?:relief|easing|ceasefire|rate\s+cuts?)\b', 'denied move', text, flags=re.I)


def has_supply_recovery_fact(text: str) -> bool:
    return bool(re.search(r'(?:공급|수출|통항|선적|송유관).{0,18}(?:재개|정상화|복구\s*완료)|(?:supply|exports?|loadings?|pipeline|transit).{0,24}(?:resum|restor|reopen)', mask_negated_moves(text), re.I)) and not bool(re.search(r'(?:재개|정상화).{0,8}(?:예정|계획|가능|실패|않|못)|(?:plan|may|could|will).{0,18}(?:resum|reopen|restor)', text, re.I))


def has_divergent_index_moves(text: str) -> bool:
    subject = r'(?:코스피|코스닥|나스닥|다우|s&p\s*500|kospi|kosdaq|nasdaq|dow)'
    stop = subject + r'|유가|원유|금리|국채|vix|변동성|\boil\b|\bcrude\b|\btreasury\b|\byields?\b|[…;,·]|\.{3}'
    clauses = list(re.finditer(subject + r'(?:(?!' + stop + r').){0,36}', text, re.I))
    down = [m for m in clauses if re.search(r'하락|급락|후퇴|밀려|밀린|약세|내린|선\s*내(?:줘|준)|↓|falls?|fell|drops?|lower', m[0], re.I)]
    up = [m for m in clauses if re.search(r'상승|급등|반등|강세|오른|↑|rises?|gain|higher|climbs?|rall(?:y|ies)', m[0], re.I)]
    return any(re.match(subject, d[0], re.I)[0].lower() != re.match(subject, u[0], re.I)[0].lower() for d in down for u in up)


def inflation_release(text: str) -> dict | None:
    metric = next((m for m, pattern in [('pce', r'\bpce\b|개인소비지출'), ('cpi', r'\bcpi\b|소비자물가'), ('ppi', r'\bppi\b|생산자물가')] if re.search(pattern, text, re.I)), None)
    if not metric:
        return None
    countries = [('fr', r'프랑스|france|french'), ('de', r'독일|germany|german'), ('jp', r'일본|japan|japanese'), ('ca', r'캐나다|canada|canadian'), ('kr', r'한국|韓|korea'), ('cn', r'중국|china|chinese'), ('uk', r'영국|britain|british|\buk\b'), ('eu', r'유로존|eurozone'), ('us', r'미국|美|\bu\.?s\.?\b|american')]
    country = next((c for c, pattern in countries if re.search(pattern, text, re.I)), 'us' if metric == 'pce' else None)
    month_match = re.search(r'(?:^|[^\d])(1[0-2]|[1-9])\s*월', text)
    months = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december']
    month = int(month_match[1]) if month_match else next((i + 1 for i, m in enumerate(months) if re.search(r'\b' + m + r'\b', text, re.I)), None)
    comparison_text = re.sub(r'(?:예상|전망)(?:치|보다)?[^…;,]{0,8}(?:하회|밑돌|밑돈|낮|상회|웃돌|높)[^…;,]{0,14}(?:아냐|아니|않|없)', '부정된비교', text, flags=re.I)
    comparison_text = re.sub(r'\b(?:not|never)\s+(?:miss|exceed|undershoot|below|lower|less|higher|more|above)[^…;,]{0,24}(?:forecast|expect|estimate)\w*', 'denied comparison', comparison_text, flags=re.I)
    below = bool(re.search(r'(?:시장\s*)?(?:예상|전망|전망치|예상치).{0,10}(?:하회|밑돌|밑돈|낮|못\s*미)|(?:below|less\s+than|lower\s+than).{0,18}(?:forecast|expect|estimate)|(?:miss(?:es|ed)?|undershoot(?:s)?).{0,12}(?:forecast|expect|estimate)', comparison_text, re.I))
    above = bool(re.search(r'(?:시장\s*)?(?:예상|전망|전망치|예상치).{0,10}(?:상회|웃돌|높)|(?:above|higher\s+than|more\s+than).{0,18}(?:forecast|expect|estimate)|(?:exceed(?:s|ed)?).{0,12}(?:forecast|expect|estimate)', comparison_text, re.I))
    return {'deniedComparison': comparison_text != text, 'family': 'gdp' if re.search(r'\bgdp\b|국내총생산', text, re.I) else 'inflation', 'metric': metric, 'country': country, 'month': month, 'surprise': 'unknown' if below == above else 'below' if below else 'above'}


def factual_market_tone(headline: str) -> str | None:
    if has_divergent_index_moves(headline):
        return 'neutral'
    # Attach final stock moves to stock names, never to a preceding bond move.
    index = (r'(?:3대\s*지수|코스피|코스닥|나스닥|다우|S&P\s*500|뉴욕\s*증시|미국\s*증시|美\s*증시|미\s*증시|국내\s*증시|'
             r'nasdaq|dow|stocks?|wall\s+street)')
    qualifiers = (r'\s*(?:지수\s*)?(?:(?:동반|일제히|모두|함께|일제|전부|역시|again|all|jointly)\s*)*'
                  r'(?:\d+(?:\.\d+)?\s*%\s*(?:대\s*)?)?')
    down = bool(re.search(index + qualifiers + r'(?:하락|급락|폭락|약세|↓|falls?|fell|drops?|dropped|lower|slump|plunge|tumble)', headline, re.I))
    up = bool(re.search(index + qualifiers + r'(?:상승|급등|반등|강세|랠리|↑|rise|rises|rose|gain|gains|higher|jump|jumps|surge|surges|rall(?:y|ies)|rebound)', headline, re.I))
    up = up or bool(re.search(r'(?:살린|살아난|되살아난|회복한)\s*증시|증시\s*(?:회복|살아나)', headline, re.I))
    if down and not up:
        return 'negative'
    de_escalation = (r'(?:이란|중동|iran|트럼프|trump).{0,24}(?:긴장\s*완화|종식|종전|휴전|공격\s*(?:자제|보류|연기|철회)|'
                     r'tensions?\s+ease|holds?\s+off|ceasefire|end\s+of\s+(?:the\s+)?war)')
    if mask_negated_moves(headline) == headline and re.search(de_escalation, headline, re.I) and re.search(r'(?:유가|oil|crude).{0,40}(?:\d+(?:\.\d+)?\s*%\s*)?(?:하락|급락|falls?|drops?|slides?|tumbles?)', headline, re.I):
        return 'positive'
    # Good falls (plan S4): yields falling with stocks or a sector rising is relief, not burden.
    rate_down = re.search(r'(?:금리|국채|10년물|수익률|yields?).{0,10}(?:하락|급락|내림|뚝|falls?|drops?|eases?|slides?)', headline, re.I)
    equity_up = up or re.search(r'(?:기술주|성장주|반도체|빅테크|tech(?:\s+stocks?)?|chip\s*stocks?).{0,8}(?:강세|상승|급등|랠리|rall(?:y|ies)|jumps?|surges?|gains?)', headline, re.I)
    if rate_down and equity_up and not down and not re.search(r'반납|pares?\s+gains', headline, re.I):
        return 'positive'
    if re.search(r'급등락|등락\s*반복|출렁|whipsaw|seesaw', headline, re.I) and not (up or down):
        return 'neutral'  # swings both ways: no direction to report
    release = inflation_release(headline)
    if release and release['deniedComparison']:
        return 'neutral'
    if release and release['surprise'] != 'unknown':
        opposing_rates = bool(re.search(r'(?:금리|국채\s*금리|yield|rates?).{0,12}(?:급등|상승|인상|surge|rise)', headline, re.I))
        if release['surprise'] == 'below':
            return 'neutral' if opposing_rates else 'positive'
        return 'neutral' if up else 'negative'
    if release and re.search(r'(?:pce|cpi|ppi|물가)(?:(?!비트코인|bitcoin|증시).){0,32}(?:상승|급등|rises?|higher)', headline, re.I) and not re.search(r'둔화|완화|cool|slow', headline, re.I):
        return 'neutral' if up else 'negative'
    if mask_negated_moves(headline) != headline and re.search(r'이란|중동|iran|hormuz|제재|sanction', headline, re.I):
        return 'neutral' if up or has_supply_recovery_fact(headline) else 'negative'
    # Symmetric to "index down only -> negative" (plan S4, 2026-11-10): a reported broad-index rise with no
    # index fall is positive, even when one company or an oil/war word appears beside it. A single stock
    # falling never flips the market's own move.
    giveback = re.search(r'반납|상승폭\s*(?:축소|줄)|상승분|pares?\s+gains|gives?\s+back', headline, re.I)
    if up and not down and not giveback:
        return 'positive'
    return None


def inflation_surprise_why(release: dict) -> str | None:
    if release['surprise'] == 'below':
        return '물가가 예상보다 낮아 금리 부담을 덜 수 있는 신호입니다. 물가 자체의 상승률과 실제 금리·지수 반응은 별도로 봅니다.'
    if release['surprise'] == 'above':
        return '물가가 예상보다 높아 금리 부담을 키울 수 있는 신호입니다. 실제 금리·지수 반응은 별도로 봅니다.'
    return None
