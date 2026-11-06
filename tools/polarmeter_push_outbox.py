#!/usr/bin/env python3
"""Push outbox (보낼 알림 목록): the notifications the sender Worker may deliver, SPEC §13.3-§13.6, bundle 1-D.

Pure functions over an already assembled snapshot; no network, no files. The worker layer is
`apply_push_outbox(snapshot, now)`, which never raises: any failure leaves an empty `items` list.

What is listed (SPEC §13.3-§13.5):
  * morning forecast  - id `kr_forecast:{T}`, T = the KR trading day the forecast is for. Sent at 08:00 KST on T,
    dropped from the list at 08:55 KST. Topics: `kr_forecast_call_{ko|en}` + `kr_forecast_daily_{ko|en}` on a day with a
    direction call, `kr_forecast_daily_{ko|en}` alone on a no-call day.
  * scored result     - id `kr_result:{T}`, only for a day with a direction call whose close is scored. `sendAfter` is the
    moment the list was made, `expireAt` is 18:00 KST on T. Topics: `kr_result_{ko|en}`.
Nothing is listed while the forecast is paused or unavailable, for a day without a forecast (`no_forecast`, `void`), for
a backfilled row, or on a weekend or KRX holiday. The list keeps only items that have not expired, at most four.

Copy is SPEC §13.4, every number read from `morningForecast.current` (`copy`, `actual`, `result`) and
`morningForecast.scorecard`. The first sentence of a call-day body is the card title after its comma, word for word; the
record piece counts real forecasts only (see `record_clause`). SPEC §6.5 is checked again here at run time (same word lists as the coach brief); a notification
body may hold more than one sentence, so the digit rule applies to the body as a whole. Length limits are on the Korean text
(title 20, body 60 characters); the English text has a looser, fixed ceiling because the SPEC's own English title is longer.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import Any, Mapping

import polarmeter_coach_brief as cb
import polarmeter_market_calendar as cal
import polarmeter_morning_forecast as mf

SCHEMA_VERSION = 'polarmeter-push-outbox-v1'
KST = cal.KST_ZONE

KINDS = ('kr_forecast', 'kr_result')
LANGS = ('ko', 'en')
MAX_ITEMS = 4

SEND_TIME_KST = time(8, 0)         # SPEC §13.3: the morning forecast goes out at 08:00 on a trading day
MORNING_EXPIRE_KST = time(8, 55)   # after 08:55 a "forecast" is not sent any more
RESULT_EXPIRE_KST = time(18, 0)    # a scored result is dropped at 18:00

LIMIT_TITLE_KO = 20
LIMIT_BODY_KO = 60
LIMIT_TITLE_EN = 36
LIMIT_BODY_EN = 100

TOPIC_PATTERN = re.compile(r'^kr_(forecast_call|forecast_daily|result)_(ko|en)$')
ID_PATTERN = re.compile(r'^(kr_forecast|kr_result):(\d{4}-\d{2}-\d{2})$')
ISO_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')

SCORED_FOR_MORNING = ('issued', 'open_scored', 'closed_scored')

TITLE_MORNING = {'ko': '꽁치 예보 · 오늘 한국장', 'en': 'Ggongchi forecast · Korea today'}
TITLE_HIT = {'ko': '꽁치 예보 채점 · 적중', 'en': 'Ggongchi forecast result · Hit'}
TITLE_MISS = {'ko': '꽁치 예보 채점 · 빗나감', 'en': 'Ggongchi forecast result · Miss'}

# The lead sentence of a call-day body is the forecast card's title after the comma, word for word (SPEC §13.4 supplement):
# the card and the notification never say different things. "가능성이 높아요" is the one phrase §6.5 lets through, in the
# title table and in this lead sentence.
ALLOWED_PHRASE = '가능성이 높아요'
KO_TITLE_SPLIT = '한국장, '
EN_DAY_PREFIX = re.compile(r'^[A-Za-z]+: ')   # "Monday: Korea is set to..." when the card was made for a later day
RECORD_MIN_LIVE_CALLS = 5   # the "so far" record needs at least this many real calls
NO_CALL_LEAD = {'ko': '오늘은 방향을 내지 않아요', 'en': 'No call today'}

KO_FORMAL_ENDING = re.compile(r'(습니다|입니다|이다|한다|된다|했다)(\.|\s|$)')   # 해요체 or a number phrase, never 합쇼체/반말
EMOJI_OR_BANG = re.compile('[!\U0001F000-\U0001FFFF☀-➿]')


# ---------------------------------------------------------------- small helpers

def kst_moment(day: date, at: time) -> datetime:
    """A Seoul wall-clock time. KST has no daylight saving, so 08:00 is 23:00 UTC the day before all year round."""
    return datetime.combine(day, at, KST)


def pair(ko: str, en: str) -> dict[str, str]:
    return {'ko': ko, 'en': en}


def text_pair(node: Any) -> tuple[str, str] | None:
    if isinstance(node, Mapping) and isinstance(node.get('ko'), str) and isinstance(node.get('en'), str) \
            and node['ko'].strip() and node['en'].strip():
        return node['ko'], node['en']
    return None


def target_day(current: Mapping[str, Any]) -> date | None:
    """T, if it is a KR trading day (weekdays and KRX holidays are not)."""
    try:
        day = date.fromisoformat(str(current.get('targetSession')))
        return day if mf.kr_open_day(day) else None
    except (ValueError, mf.ForecastUnavailable):
        return None


def counts(node: Any) -> tuple[int, int] | None:
    """(calls, hits) when both are numbers that add up (0 <= hits <= calls)."""
    if not isinstance(node, Mapping):
        return None
    calls, hits = cb.num(node.get('calls')), cb.num(node.get('openHits'))
    if calls is None or hits is None or calls < 0 or hits < 0 or hits > calls:
        return None
    return int(calls), int(hits)


def record_clause(scorecard: Any) -> tuple[str, str] | None:
    """The record piece, from real forecasts only (SPEC §13.4 supplement); None leaves the piece out.

    All of the recent 20 sessions are live forecasts (`liveSessions == sessions`): '최근 14번 중 13번 적중'.
    Otherwise, at least 5 live calls so far: '지금까지 6번 중 5번 적중'. Fewer: nothing. Days recomputed from past
    data (backfill) are never counted into a sentence that says 'recent'.
    """
    if not isinstance(scorecard, Mapping):
        return None
    recent = scorecard.get('recent')
    got = counts(recent)
    sessions, live_sessions = cb.num((recent or {}).get('sessions')), cb.num((recent or {}).get('liveSessions'))
    if got and got[0] >= 1 and sessions and live_sessions == sessions:
        return f'최근 {got[0]}번 중 {got[1]}번 적중', f'{got[1]} of last {got[0]} calls right'
    live = counts(scorecard.get('live'))
    if live and live[0] >= RECORD_MIN_LIVE_CALLS:
        return f'지금까지 {live[0]}번 중 {live[1]}번 적중', f'{live[1]} of {live[0]} calls right so far'
    return None


def lead_sentence(current: Mapping[str, Any]) -> tuple[str, str] | None:
    """The card title after the comma (ko: after '한국장, '; en: without a 'Monday: ' day prefix)."""
    title = text_pair((current.get('copy') or {}).get('title'))
    if title is None or KO_TITLE_SPLIT not in title[0]:
        return None
    ko = title[0].split(KO_TITLE_SPLIT, 1)[1].strip()
    en = EN_DAY_PREFIX.sub('', title[1]).strip()
    return (ko, en) if ko and en else None


def fit(texts: list[str], limit: int) -> str | None:
    """The first candidate that fits `limit` characters (longest first), or None."""
    return next((text for text in texts if len(text) <= limit), None)


# ---------------------------------------------------------------- copy (SPEC §13.4)

def morning_copy(current: Mapping[str, Any], scorecard: Any) -> tuple[dict[str, str], dict[str, str]] | None:
    pred, copy = current.get('predicted'), current.get('copy')
    if not isinstance(pred, Mapping) or not isinstance(copy, Mapping):
        return None
    call = pred.get('openCall')
    record = record_clause(scorecard)
    if call in ('down', 'up'):
        lead_pair = lead_sentence(current)
        if lead_pair is None:
            return None
        lead = {'ko': lead_pair[0], 'en': lead_pair[1]}
        stat = text_pair(copy.get('openStat'))
        if stat is None:
            return None
        bodies = {}
        for index, lang in enumerate(LANGS):
            head = f"{lead[lang]}. {'코스피 시가' if lang == 'ko' else 'KOSPI open'} {stat[index]}"
            candidates = ([f'{head} · {record[index]}'] if record else []) + [head]   # the record is the first thing to drop
            bodies[lang] = fit(candidates, LIMIT_BODY_KO if lang == 'ko' else LIMIT_BODY_EN)
        return (TITLE_MORNING, pair(bodies['ko'], bodies['en'])) if all(bodies.values()) else None
    if call == 'none':
        chips = copy.get('chips')
        if not isinstance(chips, list) or len(chips) < 2:
            return None
        first, second = text_pair(chips[0]), text_pair(chips[1])
        if first is None or second is None:
            return None
        ko = f"{NO_CALL_LEAD['ko']}. 밤사이 {first[0]}, {second[0]}"
        en = f"{NO_CALL_LEAD['en']}. Overnight: {first[1]}, {second[1]}"
        if len(ko) > LIMIT_BODY_KO or len(en) > LIMIT_BODY_EN:
            return None
        return TITLE_MORNING, pair(ko, en)
    return None


def result_copy(current: Mapping[str, Any], scorecard: Any) -> tuple[dict[str, str], dict[str, str]] | None:
    pred, actual, result = current.get('predicted'), current.get('actual'), current.get('result')
    if not isinstance(pred, Mapping) or not isinstance(actual, Mapping) or not isinstance(result, Mapping):
        return None
    call = pred.get('openCall')
    hit = result.get('openHit')
    open_pct, close_pct = cb.num(actual.get('openPct')), cb.num(actual.get('closePct'))
    if call not in ('down', 'up') or not isinstance(hit, bool) or open_pct is None:
        return None
    record = record_clause(scorecard)
    stat = text_pair((current.get('copy') or {}).get('openStat'))
    opened = mf.fmt_pct(open_pct, 1, True)
    if hit:
        ko = f'시가 {opened}' + (f'(예보 {stat[0]})' if stat else '') + (f', 종가 {mf.fmt_pct(close_pct, 1, True)}' if close_pct is not None else '')
        en = f'Open {opened}' + (f' (forecast {stat[1]})' if stat else '') + (f', close {mf.fmt_pct(close_pct, 1, True)}' if close_pct is not None else '')
        title = TITLE_HIT
    else:
        ko = f'예보는 {mf.DIRECTION_KO[call]} 출발, 실제 시가 {opened}'
        en = f'Called a {mf.DIRECTION_EN[call]} open; actual open {opened}'
        title = TITLE_MISS
    bodies = {
        'ko': fit(([f'{ko}. {record[0]}'] if record else []) + [ko], LIMIT_BODY_KO),
        'en': fit(([f'{en}. {record[1]}'] if record else []) + [en], LIMIT_BODY_EN),
    }
    return (title, pair(bodies['ko'], bodies['en'])) if all(bodies.values()) else None


# ---------------------------------------------------------------- items

def morning_topics(has_call: bool) -> list[str]:
    kinds = ('forecast_call', 'forecast_daily') if has_call else ('forecast_daily',)
    return [f'kr_{kind}_{lang}' for lang in LANGS for kind in kinds]


def morning_item(current: Mapping[str, Any], scorecard: Any, now: datetime) -> dict[str, Any] | None:
    if current.get('state') not in SCORED_FOR_MORNING:
        return None
    day = target_day(current)
    if day is None:
        return None
    send_after, expire_at = kst_moment(day, SEND_TIME_KST), kst_moment(day, MORNING_EXPIRE_KST)
    if now >= expire_at:
        return None
    texts = morning_copy(current, scorecard)
    if texts is None:
        return None
    has_call = current['predicted']['openCall'] in ('down', 'up')
    return {'id': f'kr_forecast:{day.isoformat()}', 'kind': 'kr_forecast', 'hasCall': has_call,
            'topics': morning_topics(has_call), 'sendAfter': mf.iso_utc(send_after), 'expireAt': mf.iso_utc(expire_at),
            'title': texts[0], 'body': texts[1],
            'open': {'screen': 'home', 'focus': 'forecast', 'targetSession': day.isoformat()}}


def result_item(current: Mapping[str, Any], scorecard: Any, now: datetime) -> dict[str, Any] | None:
    if current.get('state') != 'closed_scored':
        return None
    day = target_day(current)
    if day is None:
        return None
    expire_at = kst_moment(day, RESULT_EXPIRE_KST)
    if now >= expire_at:
        return None
    texts = result_copy(current, scorecard)
    if texts is None:
        return None
    return {'id': f'kr_result:{day.isoformat()}', 'kind': 'kr_result', 'hasCall': True,
            'topics': [f'kr_result_{lang}' for lang in LANGS], 'sendAfter': mf.iso_utc(now), 'expireAt': mf.iso_utc(expire_at),
            'title': texts[0], 'body': texts[1],
            'open': {'screen': 'home', 'focus': 'scorecard', 'targetSession': day.isoformat()}}


def limit_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Earliest send time first, at most MAX_ITEMS."""
    return sorted(items, key=lambda item: (item['sendAfter'], item['id']))[:MAX_ITEMS]


def build_items(snapshot: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """The notifications to list at `now` (may raise; `apply_push_outbox` catches)."""
    block = snapshot.get('morningForecast')
    if not isinstance(block, Mapping) or block.get('status') != 'ok':   # paused and unavailable send nothing
        return []
    current = block.get('current')
    if not isinstance(current, Mapping) or current.get('origin') != 'live' or current.get('paused'):
        return []
    scorecard = block.get('scorecard')
    items = []
    for builder in (morning_item, result_item):
        item = builder(current, scorecard, now)
        if item is not None and not item_problems(item):   # one item with a copy problem never takes the others down
            items.append(item)
    return limit_items(items)


# ---------------------------------------------------------------- copy rules (SPEC §6.5, run time)

def item_problems(item: Mapping[str, Any]) -> list[str]:
    """Every §6.5 violation in an item's title and body (empty list when clean)."""
    problems: list[str] = []
    for field, limits in (('title', (LIMIT_TITLE_KO, LIMIT_TITLE_EN)), ('body', (LIMIT_BODY_KO, LIMIT_BODY_EN))):
        node = item.get(field)
        for lang, limit in zip(LANGS, limits):
            text = node.get(lang) if isinstance(node, Mapping) else None
            where = f'{field}.{lang}: {text!r}'
            if not isinstance(text, str) or not text.strip():
                problems.append(f'empty text {where}')
                continue
            if len(text) > limit:
                problems.append(f'over {limit} characters {where}')
            if EMOJI_OR_BANG.search(text):
                problems.append(f'exclamation or emoji {where}')
            if lang == 'ko':
                checked = text.replace(ALLOWED_PHRASE, '')   # the one §6.5 exception; "가능성도" and the like still fail
                problems += [f'banned {word} {where}' for word in cb.KO_BANNED + cb.KO_ACTION + cb.NAMES if word in checked]
                if cb.NAKED_TRADE.search(text):
                    problems.append(f'banned 매수/매도 without 순 {where}')
                if KO_FORMAL_ENDING.search(text):
                    problems.append(f'not 해요체 {where}')
            else:
                if cb.EN_BANNED.search(text):
                    problems.append(f'banned English {where}')
                problems += [f'banned name {name} {where}' for name in cb.NAMES if name in text]
            if field == 'body' and not cb.HAS_NUMBER.search(text):   # the body as a whole carries a number
                problems.append(f'body needs a number {where}')
    body_ko = (item.get('body') or {}).get('ko', '') if isinstance(item.get('body'), Mapping) else ''
    if item.get('kind') == 'kr_forecast' and isinstance(body_ko, str):
        lead = body_ko.split('. ')[0]
        if not lead.endswith('요'):
            problems.append(f'the lead sentence must be 해요체: {lead!r}')
        if item.get('hasCall') and not any(word in lead for word in cb.DIRECTION_WORDS):
            problems.append(f'a call needs a direction word: {lead!r}')
    return problems


# ---------------------------------------------------------------- the block

def empty_block(now: datetime | None) -> dict[str, Any]:
    return {'schemaVersion': SCHEMA_VERSION, 'generatedAt': mf.iso_utc(now) if now else None, 'items': []}


def apply_push_outbox(snapshot: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Attach `pushOutbox` (always a block; `items` is empty when there is nothing to send). Never raises."""
    moment = None
    try:
        moment = mf.as_aware(now) if now else datetime.now(timezone.utc)
        block = empty_block(moment)
        block['items'] = build_items(snapshot, moment)
        assert_public_push_outbox(block)
    except Exception:  # the push layer must never stop a publish
        try:
            block = empty_block(moment or datetime.now(timezone.utc))
        except Exception:  # pragma: no cover - last resort
            block = {'schemaVersion': SCHEMA_VERSION, 'generatedAt': None, 'items': []}
    try:
        snapshot['pushOutbox'] = block
    except Exception:  # pragma: no cover - a snapshot that cannot take a key
        pass
    return snapshot


# ---------------------------------------------------------------- public-shape check (security / pages contracts)

def assert_public_push_outbox_if_present(snapshot: Mapping[str, Any]) -> None:
    """Pre-publish check that never blocks a publish for lack of the block (SPEC §5 decision 5): old public data has no
    `pushOutbox`. A block that is there must be well formed."""
    block = snapshot.get('pushOutbox')
    if block is None:
        return
    assert_public_push_outbox(block)


def assert_public_push_outbox(block: Any) -> None:
    """Structural check of the public block; raises AssertionError."""
    assert isinstance(block, dict), 'pushOutbox must be an object'
    assert block.get('schemaVersion') == SCHEMA_VERSION, 'pushOutbox schemaVersion'
    assert block.get('generatedAt') is None or (isinstance(block['generatedAt'], str) and ISO_PATTERN.match(block['generatedAt'])), \
        'pushOutbox generatedAt'
    items = block.get('items')
    assert isinstance(items, list) and len(items) <= MAX_ITEMS, 'pushOutbox items'
    seen: set[str] = set()
    for item in items:
        assert isinstance(item, dict), 'pushOutbox item must be an object'
        match = ID_PATTERN.match(str(item.get('id')))
        assert match, f"pushOutbox item id {item.get('id')!r}"
        assert item.get('kind') == match.group(1) and item['kind'] in KINDS, 'pushOutbox item kind must match its id'
        assert item['id'] not in seen, 'pushOutbox item ids are one per day and kind'
        seen.add(item['id'])
        assert isinstance(item.get('hasCall'), bool), 'pushOutbox hasCall'
        topics = item.get('topics')
        assert isinstance(topics, list) and topics and len(set(topics)) == len(topics), 'pushOutbox topics'
        for topic in topics:
            found = TOPIC_PATTERN.match(str(topic))
            assert found, f'pushOutbox topic {topic!r}'
            if item['kind'] == 'kr_result':
                assert found.group(1) == 'result', 'a result item sends to result topics only'
            else:
                assert found.group(1) != 'result', 'a forecast item sends to forecast topics only'
                assert item['hasCall'] or found.group(1) != 'forecast_call', 'a no-call day has no call topic'
        for lang in LANGS:
            assert any(topic.endswith(f'_{lang}') for topic in topics), f'pushOutbox topics need {lang}'
        for field in ('title', 'body'):
            node = item.get(field)
            assert isinstance(node, dict) and all(isinstance(node.get(lang), str) and node[lang] for lang in LANGS), \
                f'pushOutbox {field} needs ko and en'
        for field in ('sendAfter', 'expireAt'):
            assert isinstance(item.get(field), str) and ISO_PATTERN.match(item[field]), f'pushOutbox {field}'
        assert item['sendAfter'] < item['expireAt'], 'pushOutbox sendAfter must precede expireAt'
        opened = item.get('open')
        assert isinstance(opened, dict) and opened.get('screen') == 'home' and opened.get('focus') in ('forecast', 'scorecard') \
            and opened.get('targetSession') == match.group(2), 'pushOutbox open'
