"""Delivery-backlog guard for optional bridge generation.

The worldserver speaks at most ``Delivery.BatchSize`` lines per delivery
tick and expires lines 60 seconds past their scheduled time. When the
outbound table already holds more due lines than delivery can drain
soon, generating more optional ambient or idle chatter only pays for
lines that are likely to expire. This guard lets the bridge postpone
that optional work. Player replies and gameplay events are never gated
here; they keep flowing through the normal event path.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_BACKLOG_LIMIT = 40
DEFAULT_CHECK_SECONDS = 5

_lock = threading.Lock()
_cached_at = 0.0
_cached_due = 0
_was_exceeded = False


def _config_int(config, key, default, minimum=0):
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def backlog_limit(config):
    """Due-line threshold; zero disables the guard."""
    return _config_int(
        config,
        'LLMChatter.Delivery.GenerationBacklogLimit',
        DEFAULT_BACKLOG_LIMIT,
    )


def _query_due_rows(db):
    cursor = db.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM llm_chatter_messages "
        "WHERE delivered = 0 AND deliver_at <= NOW()"
    )
    row = cursor.fetchone()
    return int(row[0] or 0) if row else 0


def due_backlog(db, config, now=None):
    """Return the cached number of due, undelivered message rows."""
    global _cached_at, _cached_due
    now = time.monotonic() if now is None else now
    interval = _config_int(
        config,
        'LLMChatter.Delivery.BacklogCheckSeconds',
        DEFAULT_CHECK_SECONDS,
        minimum=1,
    )
    with _lock:
        if _cached_at and now - _cached_at < interval:
            return _cached_due
    started = time.monotonic()
    try:
        due = _query_due_rows(db)
    except Exception:
        # Never block generation because the probe itself failed.
        logger.warning(
            "Delivery backlog probe failed", exc_info=True
        )
        due = 0
    try:
        from chatter_metrics import record_db_timing
        record_db_timing(
            'delivery_backlog',
            (time.monotonic() - started) * 1000,
        )
    except Exception:
        pass
    with _lock:
        _cached_at = now
        _cached_due = due
    return due


def optional_generation_blocked(db, config, now=None):
    """True when optional chatter should wait for delivery to drain."""
    global _was_exceeded
    limit = backlog_limit(config)
    if not limit:
        return False
    due = due_backlog(db, config, now)
    exceeded = due >= limit
    if exceeded != _was_exceeded:
        _was_exceeded = exceeded
        if exceeded:
            logger.info(
                "Delivery backlog %d >= %d: pausing optional "
                "ambient/idle generation", due, limit,
            )
        else:
            logger.info(
                "Delivery backlog %d < %d: optional generation "
                "resumed", due, limit,
            )
    return exceeded


def reset_cache():
    """Test helper: forget the cached probe."""
    global _cached_at, _cached_due, _was_exceeded
    with _lock:
        _cached_at = 0.0
        _cached_due = 0
        _was_exceeded = False
