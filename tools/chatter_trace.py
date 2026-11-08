"""Structured, content-free decision tracing for Chatter 2.0.

One JSON line per reactive decision on the ``chatter.trace`` logger when
``LLMChatter.Trace.Enable = 1``: why a bot was selected or a turn
suppressed, which knowledge categories were used, freshness, lookup
failures and timings. Message bodies, prompts, names of private
characters and quest-log contents are never recorded. Counters always go
to ``chatter_metrics`` so the periodic diagnostics summary sees them even
when per-decision tracing is off.
"""

import json
import logging
import time

logger = logging.getLogger('chatter.trace')
_enabled = False
_ALLOWED = {
    'event_id', 'surface', 'decision', 'reason', 'bot_guid', 'addressed', 'scores', 'intents',
    'knowledge', 'evidence', 'fresh', 'lookup_ms', 'lookup_failed', 'cache_hits', 'model_ms',
    'deadline_ms', 'claims', 'fallback', 'outcome', 'circuit', 'queue_depth', 'total_ms',
}


def configure(config):
    global _enabled
    _enabled = str(config.get('LLMChatter.Trace.Enable', '0')) == '1'


def enabled():
    return _enabled


def record(**fields):
    """Count the outcome; emit a trace line only when tracing is enabled."""
    try:
        from chatter_metrics import record_skip
        if fields.get('decision') == 'silent':
            record_skip(f"social.{fields.get('reason', 'silent')}")
        elif fields.get('outcome'):
            record_skip(f"reply.{fields['outcome']}")
    except Exception:
        pass
    if not _enabled:
        return
    safe = {k: v for k, v in fields.items() if k in _ALLOWED}
    safe['ts'] = round(time.time(), 3)
    logger.info(json.dumps(safe, ensure_ascii=False, default=str, separators=(',', ':')))
