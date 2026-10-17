"""Plan cache for a small server: identical requests share one answer, and heavy planning runs one at a time.

Why: a plan is CPU-bound pure Python. On a small instance (e.g. a free Render service) several plans running at once, or the
same plan re-computed for every viewer and every page reload, starves the whole server. Here the first caller computes, anyone
asking for the same thing meanwhile waits for that result, and results are reused for a few minutes.
Standard library only; thread-safe.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone


def _strip(value):
    """Drop 'timestamp' keys: they record when a click happened and do not change the plan."""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != 'timestamp'}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def plan_key(request, now=None) -> str:
    """Stable key for a plan request. Without an explicit departure the plan departs at the current hour, so the hour is part of the key."""
    r = _strip(request)
    if not r.get('events'):
        r.pop('events', None)   # no events and an empty list are the same request
    if not r.get('departure'):
        r['_hour'] = (now or datetime.now(timezone.utc)).strftime('%Y%m%d%H')
    return json.dumps(r, sort_keys=True, default=str, separators=(',', ':'))


class PlanCache:
    def __init__(self, ttl_s=600, max_entries=32, slots=1, copy_result=False, clock=time.monotonic):
        self._ttl, self._max, self._copy, self._clock = ttl_s, max_entries, copy_result, clock
        self._lock = threading.Lock()
        self._entries = OrderedDict()   # key -> (stored at, value), least recently used first
        self._inflight = {}             # key -> Event set when that computation finishes
        self._slots = threading.BoundedSemaphore(slots) if slots else None

    def _fresh(self, key):   # caller holds the lock
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry[0] >= self._ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry

    def _out(self, value):
        return copy.deepcopy(value) if self._copy else value

    def _run(self, fn):
        if self._slots is None:
            return fn()
        with self._slots:
            return fn()

    def clear(self):
        with self._lock:
            self._entries.clear()

    def exclusive(self, fn):
        """Run fn (not cached) in one of the compute slots, so it never runs alongside other heavy plans."""
        return self._run(fn)

    def get_or_compute(self, key, fn):
        """Returns (value, hit). The first caller for a key computes; concurrent callers wait for it and get a hit.
        If the first caller fails, nothing is cached and a waiting caller tries again."""
        while True:
            with self._lock:
                entry = self._fresh(key)
                if entry is not None:
                    return self._out(entry[1]), True
                waiting = self._inflight.get(key)
                if waiting is None:
                    done = self._inflight[key] = threading.Event()
                    break
            waiting.wait()
        try:
            value = self._run(fn)
            with self._lock:
                self._entries[key] = (self._clock(), value)
                self._entries.move_to_end(key)
                while len(self._entries) > self._max:
                    self._entries.popitem(last=False)
            return self._out(value), False
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            done.set()
