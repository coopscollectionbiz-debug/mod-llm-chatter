"""Lightweight, optional bridge counters.

Counters are plain integers behind one lock; recording costs a dict
update. Nothing is logged per message. When
``LLMChatter.Diagnostics.IntervalSeconds`` is above zero the bridge loop
logs one summary line per interval and resets the window.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_window_start = time.monotonic()
_counters = {}
_latency = {}
_last_report = time.monotonic()


def _bump(key, amount=1):
    _counters[key] = _counters.get(key, 0) + amount


def record_generated(channel, count=1):
    """Count a message row written to llm_chatter_messages."""
    with _lock:
        _bump('generated', count)
        _bump(f'generated.{channel or "unknown"}', count)


def record_provider_call(kind, provider, duration_ms, ok):
    """Count one provider request and its latency."""
    with _lock:
        _bump(f'provider.{kind}.calls')
        if not ok:
            _bump(f'provider.{kind}.failures')
        stats = _latency.setdefault(
            f'{provider}.{kind}', [0, 0, 0]
        )
        stats[0] += 1
        stats[1] += int(duration_ms)
        stats[2] = max(stats[2], int(duration_ms))


def record_skip(reason):
    """Count generation skipped before any provider request."""
    with _lock:
        _bump(f'skipped.{reason}')


def record_db_timing(name, duration_ms):
    """Record timing for an optional bridge-side database check."""
    with _lock:
        stats = _latency.setdefault(f'db.{name}', [0, 0, 0])
        stats[0] += 1
        stats[1] += int(duration_ms)
        stats[2] = max(stats[2], int(duration_ms))


def snapshot(reset=False):
    """Return a copy of the current window, optionally resetting it."""
    global _window_start
    with _lock:
        data = {
            'window_seconds': round(
                time.monotonic() - _window_start, 1
            ),
            'counters': dict(_counters),
            'latency': {
                key: {
                    'count': count,
                    'avg_ms': (total // count) if count else 0,
                    'max_ms': peak,
                }
                for key, (count, total, peak) in _latency.items()
            },
        }
        if reset:
            _counters.clear()
            _latency.clear()
            _window_start = time.monotonic()
    return data


def interval_seconds(config):
    try:
        return max(0, int(config.get(
            'LLMChatter.Diagnostics.IntervalSeconds', 0
        )))
    except (TypeError, ValueError):
        return 0


def maybe_report(config, extra=None):
    """Log one summary line when the configured interval elapsed."""
    global _last_report
    interval = interval_seconds(config)
    if not interval:
        return False
    now = time.monotonic()
    if now - _last_report < interval:
        return False
    _last_report = now
    data = snapshot(reset=True)
    if extra:
        data['state'] = extra
    logger.info("[DIAG] bridge stats %s", data)
    return True
