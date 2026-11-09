#!/usr/bin/env python3
"""Build the evening-brief stats table (polarmeter_evening_model.json) from Yahoo history.

Question: when S&P futures (ES=F) have moved X% at 21:00 KST from the previous
US close (16:00 New York), how often does the S&P 500 close the day on the same
side of the previous close? This is the overnight gap holding to the close, not
a forecast of the move after the open (that one is about a coin flip, recorded
here as `afterOpenSameShare`).

Days excluded, as in the live brief: FOMC decision days and futures-roll weeks.
Usage: polarmeter_evening_train.py <dir with es_1h.json, spx_1d.json from Yahoo>
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import polarmeter_evening_brief as eb


def main() -> int:
    src = Path(sys.argv[1])
    es_payload = json.loads((src / 'es_1h.json').read_text())
    spx_payload = json.loads((src / 'spx_1d.json').read_text())
    bars = eb.hourly_bars(es_payload)
    spx = eb.daily_open_close(spx_payload)
    days = sorted(spx)
    first = datetime.fromtimestamp(bars[0][0], timezone.utc).date().isoformat()
    rows = []
    for prev, day in zip(days, days[1:]):
        if day <= first:
            continue
        open_, close = spx[day]
        prev_close = spx[prev][1]
        if not (open_ and close and prev_close) or abs(open_ - prev_close) < 1e-9:
            continue  # old bars with open == previous close carry no gap
        if eb.rest_reason(date.fromisoformat(day)):
            continue
        at = datetime(*map(int, day.split('-')), eb.FEATURE_UTC_HOUR, tzinfo=timezone.utc)
        es_pct = eb.futures_move(bars, prev, at)
        if es_pct is None:
            continue
        rows.append((es_pct, (close / prev_close - 1) * 100, (close / open_ - 1) * 100))
    model = eb.stats_table(rows)
    model['period'] = f'{first[:7]} ~ {days[-1][:7]}'
    model['builtFrom'] = 'Yahoo ES=F 60m, ^GSPC 1d'
    out = Path(__file__).with_name('polarmeter_evening_model.json')
    out.write_text(json.dumps(model, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(model, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
