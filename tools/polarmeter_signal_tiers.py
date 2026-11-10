#!/usr/bin/env python3
"""Which market signals may be published without a change (shared by the worker and the freshness audit).

Core signals: a missing value or change fails the publish (the app's required groups and the temperature need them).
Auxiliary signals: a known value with an unknown change is published as a partial signal instead of stopping
every run (11-09: one empty DXY bar failed all publishes after 00:00 New York time). The worker counts consecutive
partial publishes in dataQuality.partialSignals; at the limit the audit fails again.
"""
CORE_SIGNAL_KEYS = frozenset({'sp500', 'nasdaq100', 'vix', 'usd_krw', 'kospi', 'kosdaq', 'us10y'})
AUX_SIGNAL_KEYS = frozenset({'dxy', 'gold', 'wti', 'eem', 'iwm', 'soxx', 'smh'})
PARTIAL_STREAK_LIMIT = 3
