"""Normal-mode replies to real players' whispers (Chatter 2.0).

The worldserver (``LLMChatterWhisper.cpp``) only queues a whisper after
rejecting every Playerbots command and '@' selector, binding the turn to the
exact sender/recipient pair. This handler repeats the command check, admits
at most one reply per whisper turn, and generates through the shared
reactive pipeline with a private audience: the prompt sees only this pair's
transcript and the bot's own state, never another character's private data.
Delivery revalidates the pair, the ignore list and any live claim.
"""

from contextlib import contextmanager
import logging
import re

from chatter_db import insert_chat_message
from chatter_group_state import _mark_event
from chatter_reactive import recent_transcript
from chatter_shared import calculate_dynamic_delay, get_class_name, get_gender_label, get_race_name, parse_extra_data
from chatter_trace import record as trace

logger = logging.getLogger(__name__)
_AUDIENCE = re.compile(r'whisper:[0-9]+:([0-9]+):([0-9]+)')


@contextmanager
def reserve_whisper_reply(db, event_id, turn, audience, bot_guid, config):
    """One reply per whisper turn, and none while the pair has one pending."""
    lock = 'llm:whisper:' + audience.split(':', 2)[2]
    with db.cursor() as cursor:
        cursor.execute('SELECT GET_LOCK(%s, 0)', (lock,))
        acquired = cursor.fetchone()[0] == 1
    if not acquired:
        yield False
        return
    try:
        db.commit()
        key = audience.encode('utf-8')
        with db.cursor() as cursor:
            cursor.execute(
                """SELECT 1 FROM llm_reactive_reply_turn r
                   JOIN llm_chatter_events e ON e.id = r.event_id
                   JOIN llm_chatter_messages m ON m.event_id = r.event_id
                   WHERE r.audience = %s AND e.expires_at > NOW(6) AND m.delivered = 0 LIMIT 1""",
                (key,))
            admitted = False
            if cursor.fetchone() is None:
                cursor.execute(
                    """INSERT IGNORE INTO llm_reactive_reply_turn (turn_key, event_id, audience, bot_guid)
                       SELECT %s, id, %s, %s FROM llm_chatter_events
                       WHERE id = %s AND status = 'processing' AND expires_at > NOW(6)""",
                    (turn, key, bot_guid, event_id))
                admitted = cursor.rowcount == 1
        db.commit()
        yield admitted
    finally:
        with db.cursor() as cursor:
            cursor.execute('SELECT RELEASE_LOCK(%s)', (lock,))
            cursor.fetchone()


def process_bot_player_whisper_event(db, client, config, event):
    from chatter_group import _is_playerbot_command
    from chatter_knowledge import detect_intents
    from chatter_live import parse_live
    from chatter_reply import event_deadline, generate_reactive_reply
    from chatter_social import decide_whisper

    event_id = event['id']
    extra = parse_extra_data(event.get('extra_data'), event_id, 'bot_player_whisper') or {}
    audience = extra.get('reactive_audience', '')
    turn = extra.get('reactive_turn', '')
    message = (extra.get('player_message') or '').strip()
    player_name = extra.get('player_name') or 'someone'
    match = _AUDIENCE.fullmatch(audience)
    try:
        bot_guid = int(extra.get('bot_guid') or 0)
    except (TypeError, ValueError):
        bot_guid = 0
    if (str(config.get('LLMChatter.ChatterMode', 'normal')).strip().lower() != 'normal'
            or str(config.get('LLMChatter.Whisper.Enable', '0')) != '1'
            or not match or not turn or not message or not bot_guid
            or int(match[1]) != int(event.get('subject_guid') or 0) or int(match[2]) != bot_guid):
        trace(event_id=event_id, surface='whisper', decision='silent', reason='invalid_or_disabled')
        _mark_event(db, event_id, 'skipped')
        return False
    if _is_playerbot_command(message):
        trace(event_id=event_id, surface='whisper', decision='silent', reason='playerbot_command')
        _mark_event(db, event_id, 'skipped')
        return False
    decision = decide_whisper(message)
    if not decision.respond:
        trace(event_id=event_id, surface='whisper', decision='silent', reason=decision.reason)
        _mark_event(db, event_id, 'skipped')
        return False

    with reserve_whisper_reply(db, event_id, turn, audience, bot_guid, config) as admitted:
        if not admitted:
            trace(event_id=event_id, surface='whisper', decision='silent', reason='not_admitted')
            _mark_event(db, event_id, 'skipped')
            return False
        try:
            cursor = db.cursor(dictionary=True)
            cursor.execute('SELECT name, class, race, level, gender FROM characters WHERE guid = %s', (bot_guid,))
            row = cursor.fetchone()
            cursor.close()
            if not row:
                _mark_event(db, event_id, 'skipped')
                return False
            speaker = {'guid': bot_guid, 'name': row['name'], 'class': get_class_name(row['class']),
                       'race': get_race_name(row['race']), 'level': row['level'],
                       'gender': get_gender_label(row['gender'])}
            intents = (detect_intents(message)
                       if str(config.get('LLMChatter.Knowledge.Enable', '1')) == '1' else [])
            result = generate_reactive_reply(
                client=client, config=config, speaker=speaker, channel='whisper', asker=player_name,
                message=message, transcript=recent_transcript(db, audience),
                audience_note=f'Private whisper between you and {player_name}; nobody else can read it.',
                live=parse_live(extra), intents=intents, deadline=event_deadline(db, event_id),
                label='bot_player_whisper', allow_fallback=True)
            fields = dict(event_id=event_id, surface='whisper', bot_guid=bot_guid,
                          knowledge=result.stats.get('knowledge'), evidence=result.stats.get('evidence'),
                          model_ms=result.stats.get('model_ms'), claims=bool(result.claims),
                          fallback=result.fallback, total_ms=result.stats.get('total_ms'))
            if not result.message:
                trace(outcome='silent_reply', **fields)
                _mark_event(db, event_id, 'skipped')
                return False
            delay = calculate_dynamic_delay(len(result.message), config,
                                            prev_message_length=len(message), responsive=True)
            queued = insert_chat_message(
                db, bot_guid, speaker['name'], result.message, channel='whisper', delay_seconds=delay,
                event_id=event_id, player_guid=int(event.get('subject_guid') or 0),
                owner_subsystem='whisper', delivery_policy='responsive',
                delivery_reason='bot_player_whisper', claim_json=result.claims)
            if queued is None:
                trace(outcome='not_queued', **fields)
                _mark_event(db, event_id, 'skipped')
                return False
            trace(outcome='queued', **fields)
            _mark_event(db, event_id, 'completed')
            return True
        except Exception:
            logger.error('whisper reply failed event=%s', event_id, exc_info=True)
            _mark_event(db, event_id, 'skipped')
            return False
