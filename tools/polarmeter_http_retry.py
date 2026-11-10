#!/usr/bin/env python3
"""Small retry wrapper for the free public quote endpoints.

One HTTP 429 or a slow reply from Yahoo used to drop a signal for the whole
30-minute run (and, for a core signal, the whole publish). Only transient
failures are retried: HTTP 429/5xx, timeouts and connection errors. A 4xx
other than 429 or a bad body is returned to the caller at once.
"""
from __future__ import annotations

import socket
import time
import urllib.error
from typing import Callable, TypeVar

T = TypeVar('T')
RETRY_DELAYS_SECONDS = (1.0, 3.0)
RETRY_HTTP_CODES = {429, 500, 502, 503, 504}


def is_transient(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRY_HTTP_CODES
    if isinstance(error, urllib.error.URLError):
        return isinstance(error.reason, (TimeoutError, socket.timeout, ConnectionError, OSError))
    return isinstance(error, (TimeoutError, socket.timeout, ConnectionError))


def with_retry(call: Callable[[], T], delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
               sleep: Callable[[float], None] = time.sleep) -> T:
    """Run call(); on a transient failure wait and try again, at most len(delays) more times."""
    for delay in delays:
        try:
            return call()
        except Exception as error:  # noqa: BLE001 - re-raised unless transient
            if not is_transient(error):
                raise
            sleep(delay)
    return call()
