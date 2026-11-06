#!/usr/bin/env python3
"""Coach brief (꽁치 조언): four server-written lines - 지금, 근거, 기준선, 다음 (SPEC §4.6, §6.4, §6.5).

Pure functions over an already assembled snapshot; no network, no files. The worker layer is
`apply_coach_brief(snapshot, now)`, which never raises: any failure leaves `coachBrief: None`
and the app keeps its old advice.

Where the SPEC text and the SPEC rules (§6.5) disagree, the rules win (see the 1-C report):
a verdict is at most 28 characters and carries a direction word, no sentence says 매수/매도
(not even as 순매수/순매도), and every line other than the verdict carries a digit or a time.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping

import polarmeter_market_calendar as cal
import polarmeter_morning_forecast as mf

SCHEMA_VERSION = 'polarmeter-coach-brief-v1'
KST = cal.KST_ZONE
NY = cal.NY_ZONE
MINUS = mf.MINUS

RULE_IDS = tuple(f'C{i}' for i in range(14))
LINE_RULE_IDS = ('L1', 'L2', 'L3', 'L4', 'L5')
TONES = ('minus', 'plus', 'neutral')
ZONES = ('pre_open', 'kr_open', 'kr_closed', 'us_open', 'weekend')

MAX_EVIDENCE = 3
MAX_LINES = 2
MAX_NEXT = 2

# Evidence (SPEC §4.6 "근거"): strength = |change| / base; below 1 it is not worth saying.
TEMP_BASE = 5.0          # degrees
FX_BASE = 0.4            # percent, the "weaker won" rule of linkSignals
FLOW_BASE = 1.0          # times the typical day
VIX_CHANGE_BASE = 5.0    # percent
VIX_LEVEL_BASE = 25.0    # points
KOSPI_BASE = 1.0         # percent
CANDIDATE_ORDER = ('us_temp', 'kr_temp', 'usd_krw', 'foreign_flow', 'vix', 'kospi')

# Verdict thresholds (SPEC §4.6 C1-C6).
SPY_SAME_DIRECTION = 1.5
SOXX_SAME_DIRECTION = 2.5

# Baselines (SPEC §4.6 L1-L5): the thresholds the app already uses elsewhere.
FX_LINE_FRACTION = 0.004     # linkSignals marks the won "weak" at +0.4%
VIX_LINES = (30.0, 20.0)
FLOW_HISTORY_DAYS = 60       # same window as `link_signals`

# Freshness of the live quotes the intraday sentences lean on.
KOSPI_MAX_AGE = timedelta(hours=2)
US_QUOTE_MAX_AGE = timedelta(hours=3)

# Length limits (SPEC §6.5), in characters.
LIMIT_VERDICT = 28
LIMIT_EVIDENCE = 24
LIMIT_LINE = 30
LIMIT_NEXT = 26

# ---------------------------------------------------------------- copy rules (SPEC §6.5)

KO_BANNED = ('확인', '점검', '살펴', '지켜', '참고', '가능성', '수 있어요', '다음 갱신')
KO_ACTION = ('매수', '매도', '사세요', '파세요', '비중', '목표가', '손절', '추천')
NAMES = ('삼성전자', 'SK하이닉스', '하이닉스', '엔비디아', '테슬라', '애플', 'NVIDIA', 'Tesla', 'Apple', 'Samsung',
         'SPY', 'SOXX', 'EWY')
EN_BANNED = re.compile(r'\b(watch|monitor|may|could|buy|buying|bought|sell|selling|sold|stock\s+pick)\b'
                       r'|keep an eye|recommend', re.I)
DIRECTION_WORDS = ('내려', '올라', '밀려', '끌어', '팔렸', '샀', '조용')
KO_SENTENCE_END = re.compile(r'요\.?$')
KO_HAS_SENTENCE = re.compile(r'요(\.|$)')   # a baseline meaning may close with a noun phrase: "구간이에요. 외국인 자금에 부담"
TIME_TEXT = re.compile(r'^\d{2}:\d{2}$')


# ---------------------------------------------------------------- small helpers

def num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_instant(value: Any) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def first_fit(candidates: list[str], limit: int) -> str:
    """The first candidate that fits `limit` characters; the shortest one when none does."""
    for text in candidates:
        if len(text) <= limit:
            return text
    return min(candidates, key=len)


def unsigned_pct(value: float, decimals: int = 1) -> str:
    return mf.fmt_pct(abs(value), decimals)


def flat(value: float) -> bool:
    """A move that reads 0.0% at one decimal."""
    return round(abs(value), 1) == 0


def tone_of_sign(value: float) -> str:
    return 'neutral' if flat(value) else 'minus' if value < 0 else 'plus'


def amount_ko(value: float, with_won: bool = True) -> str:
    """`value` in 억 원."""
    v = abs(value)
    text = f'{v / 10000:.1f}조' if v >= 10000 else f'{round(v):,}억'
    return text + (' 원' if with_won else '')


def amount_en(value: float) -> str:
    v = abs(value)
    return f'{v / 10000:.1f}T won' if v >= 10000 else f'{round(v / 10):,}B won'


# ---------------------------------------------------------------- context

class Context:
    """Everything the builders read, resolved once (zone, target day, the snapshot pieces)."""

    def __init__(self, snapshot: Mapping[str, Any], now: datetime) -> None:
        self.now = mf.as_aware(now)
        self.kst = self.now.astimezone(KST)
        self.today = self.kst.date()
        block = snapshot.get('morningForecast')
        self.ok = isinstance(block, Mapping) and block.get('status') == 'ok'
        self.block: Mapping[str, Any] = block if self.ok else {}
        current = self.block.get('current')
        self.cur: Mapping[str, Any] = current if isinstance(current, Mapping) else {}
        self.target: str = str(self.cur.get('targetSession') or '')
        self.phase: str = str(self.block.get('phase') or '')
        self.zone = self._zone()
        self.signals: Mapping[str, Any] = _mapping(snapshot.get('signals'))
        self.link: list[Any] = snapshot.get('linkSignals') if isinstance(snapshot.get('linkSignals'), list) else []
        self.flows = snapshot.get('investorFlows')
        self.score = _mapping(snapshot.get('scoreCoreV3'))
        self.macro = _mapping(snapshot.get('macroEvents'))

    def _zone(self) -> str | None:
        if not self.ok or not self.cur or not self.target:
            return None
        target_day = date.fromisoformat(self.target)
        if self.phase in ('us_open', 'kr_open', 'kr_closed'):
            return self.phase
        if self.phase == 'pre_open':  # 'pre_open' for a day that is not today is the weekend / holiday card
            return 'pre_open' if target_day == self.today else 'weekend'
        return None

    @property
    def predicted(self) -> Mapping[str, Any]:
        value = self.cur.get('predicted')
        return value if isinstance(value, Mapping) else {}

    @property
    def inputs(self) -> Mapping[str, Any]:
        value = self.cur.get('inputs')
        return value if isinstance(value, Mapping) else {}

    @property
    def open_call(self) -> str | None:
        call = self.predicted.get('openCall')
        return call if call in ('down', 'up', 'none') else None

    def flow_day(self) -> str | None:
        """The KR session whose investor flows belong to this card."""
        if self.zone in ('kr_open', 'kr_closed', 'us_open'):
            return self.target
        return str(self.cur.get('basisKrSession') or '') or None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def usable_signal(ctx: Context, key: str) -> Mapping[str, Any] | None:
    signal = ctx.signals.get(key)
    if isinstance(signal, Mapping) and signal.get('status') in ('ok', 'suspect'):
        return signal
    return None


# ---------------------------------------------------------------- facts

def kospi_fact(ctx: Context) -> dict[str, float | None] | None:
    """KOSPI's change from the previous close, only while it belongs to the target session (SPEC §4.6 자료)."""
    if ctx.zone not in ('kr_open', 'kr_closed'):
        return None
    signal = usable_signal(ctx, 'kospi')
    if signal is None:
        return None
    pct, as_of = num(signal.get('changePct')), parse_instant(signal.get('dataAsOf'))
    if pct is None or as_of is None:
        return None
    local = as_of.astimezone(KST)
    if local.date().isoformat() != ctx.target:
        return None
    if local.time() < (time(15, 30) if ctx.zone == 'kr_closed' else time(9, 0)):
        return None
    if ctx.zone == 'kr_open' and not timedelta(0) <= ctx.now - as_of <= KOSPI_MAX_AGE:
        return None
    return {'pct': pct, 'prev': num(signal.get('previousClose'))}


def us_live_fact(ctx: Context, key: str) -> float | None:
    signal = usable_signal(ctx, key)
    if signal is None:
        return None
    pct, as_of = num(signal.get('changePct')), parse_instant(signal.get('dataAsOf'))
    if pct is None or as_of is None or not timedelta(minutes=-10) <= ctx.now - as_of <= US_QUOTE_MAX_AGE:
        return None
    return pct


def link_row(ctx: Context, key: str) -> Mapping[str, Any] | None:
    for row in ctx.link:
        if isinstance(row, Mapping) and row.get('key') == key:
            return row
    return None


def fx_fact(ctx: Context) -> dict[str, float | None] | None:
    """USD/KRW: value, previous close, change percent."""
    signal = usable_signal(ctx, 'usd_krw')
    value = pct = prev = None
    if signal is not None:
        value, pct, prev = num(signal.get('value')), num(signal.get('changePct')), num(signal.get('previousClose'))
    if value is None:
        row = link_row(ctx, 'usd_krw')
        if row is not None:
            value = num(re.sub(r'[^0-9.\-]', '', str(row.get('value') or '')))
            pct = num(row.get('changePct'))
    if value is None or value <= 0:
        return None
    if prev is None and pct is not None and pct > -100:
        prev = value / (1 + pct / 100)
    if pct is None and prev:
        pct = (value / prev - 1) * 100
    if pct is None:
        return None
    return {'value': value, 'pct': pct, 'prev': prev}


def vix_fact(ctx: Context) -> dict[str, float] | None:
    signal = usable_signal(ctx, 'vix')
    if signal is None:
        return None
    value, pct = num(signal.get('value')), num(signal.get('changePct'))
    if value is None or value <= 0 or pct is None:
        return None
    return {'value': value, 'pct': pct}


def flow_nets(ctx: Context) -> dict[str, tuple[float, bool]]:
    """date -> (foreign net buying of KOSPI + KOSDAQ in 억 원, settled)."""
    items = ctx.flows.get('items') if isinstance(ctx.flows, Mapping) else None
    nets: dict[str, tuple[float, bool]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get('date'), str):
            continue
        parts = [num(_mapping(item.get(market)).get('foreign')) for market in ('kospi', 'kosdaq')]
        if all(part is None for part in parts):
            continue
        nets[item['date']] = (sum(part or 0.0 for part in parts), bool(item.get('settled')))
    return nets


def flow_fact(ctx: Context) -> dict[str, Any] | None:
    """Foreign net flow for the card's KR session: net, a typical day (as `link_signals` computes it), the ratio,
    how many sessions in a row it has gone the same way, and whether the day is settled."""
    day = ctx.flow_day()
    if not day:
        return None
    nets = flow_nets(ctx)
    if day in nets:
        net, settled = nets[day]
        history = [nets[d][0] for d in sorted(nets) if d < day][-FLOW_HISTORY_DAYS:]
        typical = math.sqrt(sum(v * v for v in history) / len(history)) if history else None
        streak = 0
        for d in reversed([d for d in sorted(nets) if d <= day]):
            value = nets[d][0]
            if value != 0 and (value > 0) == (net > 0):
                streak += 1
            else:
                break
        return {'net': net, 'typical': typical, 'ratio': abs(net) / typical if typical else None,
                'streak': streak, 'settled': settled}
    row = link_row(ctx, 'foreign_flow')  # fallback: the already published row, same day only
    if row is None or row.get('asOf') != day:
        return None
    net = num(re.sub(r'[^0-9.\-]', '', str(row.get('value') or '').replace('+', '')))
    if net is None:
        return None
    match = re.search(r'평소 규모의 ([0-9.]+)배', str(row.get('readKo') or ''))
    ratio = num(match.group(1)) if match else None
    return {'net': net, 'typical': abs(net) / ratio if ratio else None, 'ratio': ratio, 'streak': 1,
            'settled': '장중' not in str(row.get('readKo') or '')}


def temperature_pair(ctx: Context, market: str) -> tuple[float, float] | None:
    delta = _mapping(_mapping(ctx.score.get('dailyDelta')).get(market))
    current, previous = num(delta.get('currentScore')), num(delta.get('previousScore'))
    if current is None or previous is None:
        return None
    return previous, current


# ---------------------------------------------------------------- 지금 (verdict)

def verdict(rule_id: str, tone: str, ko: str, en: str) -> dict[str, str]:
    return {'ruleId': rule_id, 'tone': tone, 'ko': ko, 'en': en}


PRE_TEXT = {
    # rule, down: (ko, en), up: (ko, en). The table in SPEC §4.6 plus the direction words §6.5 asks for.
    'C1': ('C4', '미국 급락이 그대로 밀려오는 날이에요', 'A sharp US drop carries straight into Korea',
           '미국 급등에 그대로 올라타는 날이에요', 'A sharp US rally carries straight into Korea'),
    'C2': ('C5', '미국 반도체가 한국장을 끌어내리는 날이에요', 'US chips are pulling Korea lower',
           '미국 반도체가 한국장을 끌어올리는 날이에요', 'US chips are pulling Korea higher'),
    'C3': ('C6', '밤사이 한국 주식만 따로 팔렸어요', 'Korean stocks alone were dumped overnight',
           '밤사이 한국 주식만 따로 샀어요', 'Korean stocks alone were bid up overnight'),
    'C0': ('C0', '밤사이 미국장 약세가 밀려오는 날이에요', 'Weakness in US stocks carries over to Korea',
           '밤사이 미국장 강세에 올라타는 날이에요', 'Strength in US stocks carries over to Korea'),
}


def pre_verdict(ctx: Context) -> dict[str, str] | None:
    """C1-C7, C0: the forecast itself. Down side first, same order for the up side."""
    call = ctx.open_call
    if call is None or ctx.cur.get('state') in ('no_forecast', 'void', 'pending'):
        return None
    if call == 'none':
        return verdict('C7', 'neutral', '미국장은 조용했어요. 한국 안쪽 재료가 정해요',
                       'US stocks were quiet. Korea-specific news decides')
    down = call == 'down'
    spy, soxx, excess = (num(ctx.inputs.get(key)) for key in ('spyPct', 'soxxPct', 'ewyExcessPct'))

    def same(value: float | None, threshold: float) -> bool:
        return value is not None and (value <= -threshold if down else value >= threshold)

    if same(spy, SPY_SAME_DIRECTION):
        key = 'C1'
    elif same(soxx, SOXX_SAME_DIRECTION):
        key = 'C2'
    elif ctx.predicted.get('topDriver') == 'ewyExcessPct' and same(excess, 0.0) and excess != 0:
        key = 'C3'
    else:
        key = 'C0'
    up_id, down_ko, down_en, up_ko, up_en = PRE_TEXT[key]
    return verdict(key if down else up_id, 'minus' if down else 'plus', down_ko if down else up_ko,
                   down_en if down else up_en)


def open_verdict(ctx: Context) -> dict[str, str] | None:
    """C8-C10 once the open is scored and the forecast made a call; otherwise None (the caller falls back)."""
    result = _mapping(ctx.cur.get('result'))
    actual = _mapping(ctx.cur.get('actual'))
    call, hit, open_pct = ctx.open_call, result.get('openHit'), num(actual.get('openPct'))
    if ctx.cur.get('state') not in ('open_scored', 'closed_scored') or call not in ('down', 'up') or hit is None \
            or open_pct is None:
        return None
    down = call == 'down'
    if hit is False:  # C10: the open went against the call
        if open_pct == 0:
            word_ko, word_en = ('내려서' if down else '올라서'), ('lower' if down else 'higher')
            return verdict('C10', 'neutral', f'밤사이 신호와 달리 {word_ko} 시작하지 않았어요',
                           f'Did not open {word_en}, against the overnight signal')
        went_up = open_pct > 0
        return verdict('C10', 'plus' if went_up else 'minus',
                       f"밤사이 신호와 달리 {'올라서' if went_up else '내려서'} 시작했어요",
                       f"Opened {'higher' if went_up else 'lower'}, against the overnight signal")
    kospi = kospi_fact(ctx)
    start_ko, start_en = ('밀려서', 'lower') if down else ('올라서', 'higher')
    if kospi is None:
        return verdict('C8', 'minus' if down else 'plus', f'예보대로 {start_ko} 시작했어요', f'Opened {start_en}, as called')
    rel = ((1 + kospi['pct'] / 100) / (1 + open_pct / 100) - 1) * 100  # change since the open
    if flat(rel):
        return verdict('C9', 'neutral', f'{start_ko} 시작해 지금은 시가와 비슷해요',
                       f'Opened {start_en} as called and is back near the open')
    amount = unsigned_pct(rel)
    continued = rel < 0 if down else rel > 0
    if continued:  # C8
        return verdict('C8', 'minus' if down else 'plus',
                       f"예보대로 시작해 시가보다 {amount} 더 {'내려왔어요' if down else '올라왔어요'}",
                       f"As called, {amount} further {'below' if down else 'above'} the open")
    return verdict('C9', 'plus' if down else 'minus',  # C9: the open went as called, the day came back
                   f"{start_ko} 시작했지만 시가보다 {amount} {'올라왔어요' if down else '내려왔어요'}",
                   f"Opened {start_en} but now {amount} {'above' if down else 'below'} the open")


def close_pct_of(ctx: Context) -> float | None:
    actual = _mapping(ctx.cur.get('actual'))
    value = num(actual.get('closePct'))
    if value is not None:
        return value
    fact = kospi_fact(ctx)
    return fact['pct'] if fact else None


def close_verdict(ctx: Context) -> dict[str, str] | None:
    """C11. The foreign-flow half of the SPEC sentence does not fit 28 characters; it is an evidence line instead."""
    pct = close_pct_of(ctx)
    if pct is None:
        return None
    ko_day, en_day = mf.day_words(ctx.target, ctx.today)
    en_when = 'today' if en_day == 'today' else f'on {en_day}'
    if flat(pct):
        return verdict('C11', 'neutral', f'{ko_day} 코스피는 조용히 마감했어요', f'KOSPI closed {en_when} almost unchanged')
    up = pct > 0
    return verdict('C11', 'plus' if up else 'minus', f"{ko_day} 코스피 {unsigned_pct(pct)} {'올라' if up else '내려'} 마감했어요",
                   f"KOSPI closed {unsigned_pct(pct)} {'higher' if up else 'lower'} {en_when}")


def us_verdict(ctx: Context) -> dict[str, str] | None:
    """C12: the US session is open; S&P500 and, when it moves the same way and fits, US chips."""
    spx = us_live_fact(ctx, 'sp500')
    if spx is None:
        return None
    soxx = us_live_fact(ctx, 'soxx')
    if flat(spx):
        return verdict('C12', 'neutral', '미국장 S&P500은 조용해요', 'S&P 500 is flat in the US session')
    up = spx > 0
    word_ko, word_en = ('올라', 'up') if up else ('내려', 'down')
    ko = f'미국장 S&P500이 {unsigned_pct(spx)} {word_ko} 있어요'
    en = f'S&P 500 is {word_en} {unsigned_pct(spx)}'
    if soxx is not None and not flat(soxx) and (soxx > 0) == up:
        both = f"S&P500 {unsigned_pct(spx)}·반도체 {unsigned_pct(soxx)} {'올라요' if up else '내려요'}"
        if len(both) <= LIMIT_VERDICT:
            ko = both
            en = f'S&P 500 {word_en} {unsigned_pct(spx)}, US chips {word_en} {unsigned_pct(soxx)}'
    return verdict('C12', 'plus' if up else 'minus', ko, en)


def weekend_verdict(ctx: Context) -> dict[str, str] | None:
    """C13: the weekend / holiday card says how the last US session ended (the date of the next KR open is in 다음)."""
    spy = num(ctx.inputs.get('spyPct'))
    if spy is None:
        return None
    sessions = ctx.cur.get('usSessions') if isinstance(ctx.cur.get('usSessions'), list) else []
    if len(sessions) == 1:
        day = date.fromisoformat(str(sessions[0]))
        label_ko, label_en = f'{mf.WEEKDAYS_KO[day.weekday()]}요일', f'on {mf.WEEKDAYS_EN[day.weekday()]}'
    else:
        label_ko, label_en = '지난', 'in the last US session'
    if flat(spy):
        return verdict('C13', 'neutral', f'{label_ko} S&P500은 조용히 끝났어요', f'S&P 500 ended flat {label_en}')
    up = spy > 0
    return verdict('C13', 'plus' if up else 'minus',
                   f"{label_ko} S&P500은 {unsigned_pct(spy)} {'올라' if up else '내려'} 끝났어요",
                   f"S&P 500 ended {unsigned_pct(spy)} {'higher' if up else 'lower'} {label_en}")


def pick_verdict(ctx: Context) -> dict[str, str] | None:
    if ctx.zone == 'pre_open':
        return pre_verdict(ctx)
    if ctx.zone == 'kr_open':  # before the open is scored, or on a no-call day, the morning sentence still holds
        return open_verdict(ctx) or pre_verdict(ctx)
    if ctx.zone == 'kr_closed':
        return close_verdict(ctx)
    if ctx.zone == 'us_open':
        return us_verdict(ctx)
    if ctx.zone == 'weekend':
        return weekend_verdict(ctx)
    return None


# ---------------------------------------------------------------- 근거 (evidence)

def evidence_candidates(ctx: Context, skip_kospi: bool) -> list[tuple[float, int, dict[str, str]]]:
    out: list[tuple[float, int, dict[str, str]]] = []

    def add(key: str, strength: float, ko: str, en: str) -> None:
        if strength >= 1:
            out.append((strength, CANDIDATE_ORDER.index(key), {'key': key, 'ko': ko, 'en': en}))

    for key, market, name_ko, name_en in (('us_temp', 'us', '미국', 'US'), ('kr_temp', 'kr', '한국', 'Korea')):
        pair = temperature_pair(ctx, market)
        if pair:
            before, after = pair
            add(key, abs(after - before) / TEMP_BASE, f'{name_ko} 온도 {mf.fmt_num(before, 0)}° → {mf.fmt_num(after, 0)}°',
                f'{name_en} temperature {mf.fmt_num(before, 0)}° → {mf.fmt_num(after, 0)}°')
    fx = fx_fact(ctx)
    if fx:
        pct = mf.fmt_pct(fx['pct'], 1, True)
        add('usd_krw', abs(fx['pct']) / FX_BASE, f"원/달러 {mf.fmt_num(fx['value'], 0)}원({pct})",
            f"USD/KRW {mf.fmt_num(fx['value'], 0)} ({pct})")
    flow = flow_fact(ctx)
    if flow and flow['ratio'] is not None:
        sell = flow['net'] < 0
        amount, amount_short = amount_ko(flow['net']), amount_ko(flow['net'], with_won=False)
        verb = '팔았어요' if sell else '샀어요'
        ratio = f"{flow['ratio']:.1f}"
        if flow['streak'] >= 2:
            ko = first_fit([f"외국인 {flow['streak']}일째 {amount} {verb}", f"외국인 {flow['streak']}일째 {amount_short} {verb}"],
                           LIMIT_EVIDENCE)
            en = f"Foreigners {'pulled out' if sell else 'put in'} {amount_en(flow['net'])}, day {flow['streak']}"
        else:
            ko = first_fit([f'외국인 {amount} {verb}(평소의 {ratio}배)', f'외국인 {amount_short} {verb}(평소의 {ratio}배)',
                            f'외국인 {amount_short} {verb}(평소 {ratio}배)', f'외국인 {amount} {verb}'], LIMIT_EVIDENCE)
            en = f"Foreigners {'pulled out' if sell else 'put in'} {amount_en(flow['net'])} ({ratio}x usual)"
        add('foreign_flow', flow['ratio'] / FLOW_BASE, ko, en)
    vix = vix_fact(ctx)
    if vix:
        strength = max(abs(vix['pct']) / VIX_CHANGE_BASE, vix['value'] / VIX_LEVEL_BASE if vix['value'] >= VIX_LEVEL_BASE else 0)
        text = f"VIX {mf.fmt_num(vix['value'], 0)}({mf.fmt_pct(vix['pct'], 0, True)})"
        add('vix', strength, text, text)
    kospi = None if skip_kospi else kospi_fact(ctx)
    if kospi:
        text = mf.fmt_pct(kospi['pct'], 1, True)
        add('kospi', abs(kospi['pct']) / KOSPI_BASE, f'코스피 {text}', f'KOSPI {text}')
    out.sort(key=lambda item: (-item[0], item[1]))
    return out


# ---------------------------------------------------------------- 기준선 (levels)

def line(rule_id: str, tone: str, level_ko: str, level_en: str, ko: str, en: str) -> dict[str, Any]:
    return {'ruleId': rule_id, 'level': {'ko': level_ko, 'en': level_en}, 'tone': tone, 'ko': ko, 'en': en}


def line_candidates(ctx: Context) -> list[dict[str, Any]]:
    """Every baseline with a value in this zone. `gap` is how far the current value sits from the line, in percent of
    the current value (L4: percent of the typical day) - only used to order them, never shown."""
    out: list[dict[str, Any]] = []

    def add(group: str, order: int, gap: float, built: dict[str, Any]) -> None:
        out.append({'group': group, 'order': order, 'gap': gap, 'line': built})

    fx = fx_fact(ctx)
    if fx and fx['prev']:  # L1 / L2: previous close +-0.4%, the "weaker / stronger won" rule
        up_level, down_level = fx['prev'] * (1 + FX_LINE_FRACTION), fx['prev'] * (1 - FX_LINE_FRACTION)
        add('fx', 1, abs(fx['value'] - up_level) / fx['value'] * 100,
            line('L1', 'minus', f'{mf.fmt_num(up_level, 0)}원 위', f'Above {mf.fmt_num(up_level, 0)}',
                 '원화 약세 구간이에요. 외국인 자금에 부담', 'Weak-won zone: a drag on foreign money'))
        add('fx', 2, abs(fx['value'] - down_level) / fx['value'] * 100,
            line('L2', 'plus', f'{mf.fmt_num(down_level, 0)}원 아래', f'Below {mf.fmt_num(down_level, 0)}',
                 '원화 강세 구간이에요. 외국인 자금에 우호', 'Strong-won zone: supportive of foreign money'))
    kospi = kospi_fact(ctx) if ctx.zone == 'kr_open' else None
    open_pct = num(_mapping(ctx.cur.get('actual')).get('openPct'))
    if kospi and kospi['prev'] and open_pct is not None:  # L3: today's open, the side the market is not on yet
        level = kospi['prev'] * (1 + open_pct / 100)
        rel = ((1 + kospi['pct'] / 100) / (1 + open_pct / 100) - 1) * 100
        text = mf.fmt_num(level, 0)
        if rel >= 0:
            more = '더 ' if open_pct < 0 else ''
            built = line('L3', 'minus', f'{text} 아래', f'Below {text}', f'시가보다 {more}내려온 상태예요', 'Down from the open')
        else:
            more = '더 ' if open_pct > 0 else ''
            built = line('L3', 'plus', f'{text} 위', f'Above {text}', f'시가보다 {more}올라온 상태예요', 'Up from the open')
        add('open', 3, abs(rel), built)
    flow = flow_fact(ctx) if ctx.zone in ('kr_open', 'kr_closed') else None
    if flow and not flow['settled'] and flow['typical'] and flow['net'] != 0 and abs(flow['net']) < flow['typical']:
        sell = flow['net'] < 0  # L4: a day that is still building toward a typical day's size
        amount = amount_ko(flow['typical'])
        add('flow', 4, (flow['typical'] - abs(flow['net'])) / flow['typical'] * 100,
            line('L4', 'minus' if sell else 'plus', f"외국인 {amount} 넘게 {'팔면' if sell else '사면'}",
                 f"Foreign {'outflow' if sell else 'inflow'} over {amount_en(flow['typical'])}",
                 '평소보다 큰 규모예요', 'Bigger than a typical day'))
    vix = vix_fact(ctx)
    if vix:  # L5: VIX 30 and 20
        high, low = VIX_LINES
        add('vix', 5, abs(vix['value'] - high) / vix['value'] * 100,
            line('L5', 'minus', f'VIX {high:g} 위', f'VIX above {high:g}', '공포가 큰 구간이에요', 'A high-fear zone'))
        add('vix', 6, abs(vix['value'] - low) / vix['value'] * 100,
            line('L5', 'plus', f'VIX {low:g} 아래', f'VIX below {low:g}', '평소 수준이에요', 'A calm zone'))
    return out


def pick_lines(ctx: Context) -> list[dict[str, Any]]:
    """Closest to the line first; when that line belongs to an above/below pair, the pair is kept together."""
    cands = sorted(line_candidates(ctx), key=lambda item: (item['gap'], item['order']))
    if not cands:
        return []
    chosen = [cands[0]]
    rest = cands[1:]
    partners = [c for c in rest if c['group'] == cands[0]['group']]
    if partners:
        chosen.append(partners[0])
    elif rest:
        chosen.append(rest[0])
    chosen.sort(key=lambda item: item['order'])
    return [item['line'] for item in chosen[:MAX_LINES]]


# ---------------------------------------------------------------- 다음 (schedule)

MACRO_EN = {'us_nonfarm_payrolls': 'US jobs report', 'us_cpi': 'US CPI report', 'fomc_rate': 'FOMC rate decision'}


def when_words(moment: datetime, today: date) -> tuple[str, str]:
    day = moment.astimezone(KST).date()
    delta = (day - today).days
    if delta <= 0:
        return '', ''
    if delta == 1:
        return '내일 ', 'Tomorrow: '
    return f'{mf.WEEKDAYS_KO[day.weekday()]}요일 ', f'{mf.WEEKDAYS_EN[day.weekday()][:3]}: '


def schedule_events(ctx: Context) -> list[tuple[datetime, int, str, str]]:
    events: list[tuple[datetime, int, str, str]] = []
    today, call = ctx.today, ctx.open_call
    state = ctx.cur.get('state')

    def add(moment: datetime, order: int, ko: str | list[str], en: str) -> None:
        # An event whose sentence cannot fit 26 characters is left out: it must never cost the whole card its 'copy rules' pass.
        text = first_fit(ko, LIMIT_NEXT) if isinstance(ko, list) else ko
        if moment > ctx.now and len(text) <= LIMIT_NEXT:
            events.append((moment, order, text, en))

    for offset in range(0, 6):  # Korean sessions
        day = today + timedelta(days=offset)
        try:
            if not mf.kr_open_day(day):
                continue
        except mf.ForecastUnavailable:
            continue
        is_target = day.isoformat() == ctx.target
        called = is_target and call in ('down', 'up')
        opens, closes = datetime.combine(day, time(9, 0), KST), datetime.combine(day, time(15, 30), KST)
        ko_when, en_when = when_words(opens, today)
        add(opens, 0, f'{ko_when}한국장이 열려요', f'{en_when}Korea opens')
        if called and state == 'issued':
            add(datetime.combine(day, time(9, 35), KST), 0, f'{ko_when}시가 예보를 채점해요', f'{en_when}The open call is scored')
        if called and state != 'closed_scored':
            ko_close = '마감 뒤 오늘 예보를 채점해요' if day == today else f'{ko_when}마감 뒤 예보를 채점해요'
            en_close = "After the close, today's call is scored" if day == today else f'{en_when}After the close the call is scored'
            add(closes, 0, ko_close, en_close)
        else:
            add(closes, 0, f'{ko_when}한국장이 마감해요', f'{en_when}Korea closes')
    ny_today = ctx.now.astimezone(NY).date()
    for offset in range(-1, 6):  # US opens, 22:30 KST in summer time and 23:30 otherwise (9:30 New York)
        day = ny_today + timedelta(days=offset)
        if cal.us_market_closed_reason(datetime.combine(day, time(12), NY)) is not None:
            continue
        opens = datetime.combine(day, time(9, 30), NY)
        ko_when, en_when = when_words(opens, today)
        if call == 'down':
            ko, en = '미국장 시작, 반도체가 반등하는지 봐요', 'US open, to see whether chips rebound'
        elif call == 'up':
            ko, en = '미국장 시작, 반도체가 계속 오르는지 봐요', 'US open, to see whether chips keep rising'
        else:
            ko, en = '미국장이 열려요', 'The US market opens'
        add(opens, 1, [f'{ko_when}{ko}', f'{ko_when}미국장이 열려요'], f'{en_when}{en}')   # the plain sentence when the day word makes it long
    issue = parse_instant(ctx.block.get('nextIssueAt'))
    if issue is not None:
        ko_when, en_when = when_words(issue, today)
        add(issue, 2, f'{ko_when}새 예보가 나와요', f'{en_when}The next forecast comes out')
    for key, event in ctx.macro.items():
        release = _mapping(_mapping(event).get('nextRelease'))
        at, label = parse_instant(release.get('scheduledAt')), str(release.get('label') or '').strip()
        if at is None or not label or not ctx.now < at <= ctx.now + timedelta(hours=24):
            continue
        ko_when, en_when = when_words(at, today)
        add(at, 3, f'{ko_when}{label} 발표가 나와요', f"{en_when}{MACRO_EN.get(key, 'A US data release')} is out")
    events.sort(key=lambda item: (item[0], item[1]))
    return events


def pick_next(ctx: Context) -> list[dict[str, str]]:
    out = []
    for moment, _, ko, en in schedule_events(ctx)[:MAX_NEXT]:
        out.append({'at': mf.iso_utc(moment), 'atKst': moment.astimezone(KST).strftime('%H:%M'), 'ko': ko, 'en': en})
    return out


# ---------------------------------------------------------------- the block

def build_coach_brief(snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
    """The `coachBrief` block for `now`, or None when 지금 cannot be made. May raise; `apply_coach_brief` does not."""
    ctx = Context(snapshot, now)
    if ctx.zone is None:
        return None
    chosen = pick_verdict(ctx)
    if chosen is None:
        return None
    candidates = evidence_candidates(ctx, skip_kospi=chosen['ruleId'] == 'C11')  # C11 already says KOSPI's number
    evidence = [item[2] for item in candidates[:MAX_EVIDENCE]]
    return {
        'schemaVersion': SCHEMA_VERSION,
        'generatedAt': mf.iso_utc(ctx.now),
        'phase': ctx.phase,
        'ruleId': chosen['ruleId'],
        'tone': chosen['tone'],
        'verdict': {'ko': chosen['ko'], 'en': chosen['en']},
        'evidence': evidence,
        'lines': pick_lines(ctx),
        'next': pick_next(ctx),
        'evidenceCount': len(candidates),
    }


def strings_of(brief: Mapping[str, Any]):
    """(path, language, text) for every ko/en pair in the brief."""
    def walk(node: Any, path: str):
        if isinstance(node, Mapping):
            if 'ko' in node and 'en' in node and isinstance(node['ko'], str):
                yield path, 'ko', node['ko']
                yield path, 'en', node['en']
            for key, value in node.items():
                if key not in ('ko', 'en'):
                    yield from walk(value, f'{path}.{key}')
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from walk(value, f'{path}[{index}]')
    yield from walk(brief, '')


def copy_problems(brief: Mapping[str, Any]) -> list[str]:
    """Every §6.5 violation in the brief (empty list when clean)."""
    problems: list[str] = []
    for path, lang, text in strings_of(brief):
        where = f'{path}.{lang}: {text!r}'
        if not isinstance(text, str) or not text.strip():
            problems.append(f'empty text {where}')
            continue
        if '!' in text or any(ord(c) >= 0x1F000 or 0x2600 <= ord(c) <= 0x27BF for c in text):
            problems.append(f'exclamation or emoji {where}')
        if lang == 'ko':
            problems += [f'banned {word} {where}' for word in KO_BANNED + KO_ACTION + NAMES if word in text]
        else:
            if EN_BANNED.search(text):
                problems.append(f'banned English {where}')
            problems += [f'banned name {name} {where}' for name in NAMES if name in text]
    verdict_ko = (brief.get('verdict') or {}).get('ko', '')
    if len(verdict_ko) > LIMIT_VERDICT:
        problems.append(f'verdict over {LIMIT_VERDICT} characters: {verdict_ko!r}')
    if not any(word in verdict_ko for word in DIRECTION_WORDS):
        problems.append(f'verdict needs a direction word: {verdict_ko!r}')
    if not KO_SENTENCE_END.search(verdict_ko):
        problems.append(f'verdict must end in 해요체: {verdict_ko!r}')
    evidence = brief.get('evidence') or []
    if len(evidence) > MAX_EVIDENCE:
        problems.append('too many evidence items')
    for item in evidence:
        if len(item['ko']) > LIMIT_EVIDENCE:
            problems.append(f"evidence over {LIMIT_EVIDENCE} characters: {item['ko']!r}")
        for lang in ('ko', 'en'):
            if not re.search(r'\d', item[lang]):
                problems.append(f'evidence needs a number: {item[lang]!r}')
    lines = brief.get('lines') or []
    if len(lines) > MAX_LINES:
        problems.append('too many lines')
    for item in lines:
        if len(item['ko']) > LIMIT_LINE:
            problems.append(f"line meaning over {LIMIT_LINE} characters: {item['ko']!r}")
        if not KO_HAS_SENTENCE.search(item['ko']):
            problems.append(f"line meaning must be 해요체: {item['ko']!r}")
        for lang in ('ko', 'en'):  # the level carries the number: "1,345원 위: 원화 약세 구간이에요..."
            if not re.search(r'\d', item['level'][lang] + item[lang]):
                problems.append(f'line needs a number: {item[lang]!r}')
    upcoming = brief.get('next') or []
    if len(upcoming) > MAX_NEXT:
        problems.append('too many next items')
    for item in upcoming:
        if len(item['ko']) > LIMIT_NEXT:
            problems.append(f"next over {LIMIT_NEXT} characters: {item['ko']!r}")
        if not KO_SENTENCE_END.search(item['ko']):
            problems.append(f"next must end in 해요체: {item['ko']!r}")
        if not TIME_TEXT.match(str(item.get('atKst'))):  # the time is the number of a next item
            problems.append(f"next needs a time: {item!r}")
    return problems


def apply_coach_brief(snapshot: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Attach `coachBrief` (the block, or None). Never raises; a brief that breaks a copy rule is not published."""
    brief = None
    try:
        moment = mf.as_aware(now) if now else datetime.now(timezone.utc)
        brief = build_coach_brief(snapshot, moment)
        if brief is not None:
            assert_public_coach_brief(brief)
            if copy_problems(brief):
                brief = None
    except Exception:  # the coach layer must never stop a publish
        brief = None
    snapshot['coachBrief'] = brief
    return snapshot


# ---------------------------------------------------------------- public-shape check (security / pages contracts)

def assert_public_coach_brief_if_present(snapshot: Mapping[str, Any]) -> None:
    """Pre-publish check that never blocks a publish for lack of the block (SPEC §5 decision 5): old public data has no
    `coachBrief`, and the layer writes None whenever it cannot make one. A block that is there must be well formed."""
    block = snapshot.get('coachBrief')
    if block is None:
        return
    assert_public_coach_brief(block)


def assert_public_coach_brief(block: Any) -> None:
    """Structural check of the public block; raises AssertionError."""
    assert isinstance(block, dict), 'coachBrief must be an object'
    assert block.get('schemaVersion') == SCHEMA_VERSION, 'coachBrief schemaVersion'
    assert isinstance(block.get('generatedAt'), str) and block['generatedAt'].endswith('Z'), 'coachBrief generatedAt'
    assert block.get('phase') in mf.PHASES, f"coachBrief phase {block.get('phase')!r}"
    assert block.get('ruleId') in RULE_IDS, f"coachBrief ruleId {block.get('ruleId')!r}"
    assert block.get('tone') in TONES, 'coachBrief tone'

    def pair(item: Any, label: str) -> None:
        assert isinstance(item, dict) and isinstance(item.get('ko'), str) and item['ko'] and isinstance(item.get('en'), str) \
            and item['en'], f'coachBrief {label} needs ko and en'

    pair(block.get('verdict'), 'verdict')
    evidence, lines, upcoming = block.get('evidence'), block.get('lines'), block.get('next')
    assert isinstance(evidence, list) and len(evidence) <= MAX_EVIDENCE, 'coachBrief evidence'
    for item in evidence:
        pair(item, 'evidence')
        assert item.get('key') in CANDIDATE_ORDER, 'coachBrief evidence key'
    assert isinstance(lines, list) and len(lines) <= MAX_LINES, 'coachBrief lines'
    for item in lines:
        pair(item, 'line')
        pair(item.get('level'), 'line level')
        assert item.get('ruleId') in LINE_RULE_IDS and item.get('tone') in TONES, 'coachBrief line rule'
    assert isinstance(upcoming, list) and len(upcoming) <= MAX_NEXT, 'coachBrief next'
    for item in upcoming:
        pair(item, 'next')
        assert isinstance(item.get('at'), str) and item['at'].endswith('Z'), 'coachBrief next.at'
        assert isinstance(item.get('atKst'), str) and TIME_TEXT.match(item['atKst']), 'coachBrief next.atKst'
    assert isinstance(block.get('evidenceCount'), int) and block['evidenceCount'] >= len(evidence), 'coachBrief evidenceCount'
