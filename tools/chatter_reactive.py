"""Admission and transcript for source-bound reactive replies."""

import re
from contextlib import contextmanager


_ACK = re.compile(r"(?:ok(?:ay)?|k|kk|ty|thx|thanks|np|yep|yeah|nice|lol|gg|got it)(?:[ ,]+(?:ty|thx|thanks))?")
_ADDRESS = re.compile(r"^@?([\w-]+)\s*[:,]")
_ROLE_TERMS = {
    "healer": r"\b(heal|heals|mana|oom|drink|rez|res)\b",
    "tank": r"\b(tank|aggro|pull|taunt|patrol)\b",
}


def select_party_responder(message, bots, other_names=()):
    text = message.strip().casefold()
    if not text or _ACK.fullmatch(text.rstrip(".!? ")):
        return None
    names = {bot["bot_name"].casefold(): bot for bot in bots}
    match = _ADDRESS.match(text)
    if match:
        name = match[1]
        if name in names:
            return names[name], True
        if name in {name.casefold() for name in other_names} or text.startswith("@"):
            return None
    for name, bot in names.items():
        if re.match(rf"^@?{re.escape(name)}(?:\b|$)", text):
            return bot, True
    for name in other_names:
        if re.match(rf"^@?{re.escape(name.casefold())}(?:\b|$)", text):
            return None
    relevant_roles = {
        role for role, terms in _ROLE_TERMS.items() if re.search(terms, text)
    }
    if not bots or ("?" not in text and not relevant_roles
                    and not re.search(r"\b(wait|left|right|interrupt|incoming|los|run)\b", text)):
        return None
    return min(bots, key=lambda bot: (
        (bot.get("role") or "").casefold() not in relevant_roles,
        bot.get("last_reply") or 0,
        bot["bot_guid"],
    )), False


def recent_transcript(db, audience, limit=8):
    with db.cursor(dictionary=True) as cursor:
        cursor.execute(
            """SELECT speaker_name, is_bot, message, speaker_guid
               FROM llm_reactive_chat_history WHERE audience = %s
               ORDER BY id DESC LIMIT %s""",
            (audience.encode("utf-8"), limit),
        )
        return list(reversed(cursor.fetchall()))


@contextmanager
def reserve_party_reply(db, event_id, turn, audience, bot_guid, direct, config):
    """Hold one DB-session lock per party and consume a turn durably before generation."""
    lock = "llm:party:" + audience.split(":")[2]
    with db.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK(%s, 0)", (lock,))
        acquired = cursor.fetchone()[0] == 1
    if not acquired:
        yield False
        return
    try:
        # A selection query may already have opened a REPEATABLE READ
        # snapshot. Check for previous workers' writes after taking the lock.
        db.commit()
        party_gap = max(0, int(config.get("LLMChatter.PartyGate.ReactivePartyGapSeconds", 6)))
        bot_gap = max(0, int(config.get("LLMChatter.PartyGate.ReactiveBotGapSeconds", 12)))
        audience_key = audience.encode("utf-8")
        with db.cursor() as cursor:
            cursor.execute(
                """SELECT 1
                   FROM llm_reactive_reply_turn r
                   JOIN llm_chatter_events e ON e.id = r.event_id
                   JOIN llm_chatter_messages m ON m.event_id = r.event_id
                   WHERE r.audience = %s AND e.expires_at > NOW(6) AND m.delivered = 0
                   LIMIT 1""",
                (audience_key,),
            )
            pending = cursor.fetchone() is not None
            cooling = False
            if not pending and not direct:
                cursor.execute(
                    """SELECT 1 FROM llm_reactive_reply_turn WHERE audience = %s AND (
                         admitted_at > DATE_SUB(NOW(6), INTERVAL %s SECOND)
                         OR (bot_guid = %s AND admitted_at > DATE_SUB(NOW(6), INTERVAL %s SECOND))
                       ) LIMIT 1""",
                    (audience_key, party_gap, bot_guid, bot_gap),
                )
                cooling = cursor.fetchone() is not None
            admitted = False
            if not pending and not cooling:
                cursor.execute(
                    """INSERT IGNORE INTO llm_reactive_reply_turn
                       (turn_key, event_id, audience, bot_guid)
                       SELECT %s, id, %s, %s FROM llm_chatter_events
                       WHERE id = %s AND status = 'processing' AND expires_at > NOW(6)""",
                    (turn, audience_key, bot_guid, event_id),
                )
                admitted = cursor.rowcount == 1
        db.commit()
        yield admitted
    finally:
        with db.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (lock,))
            cursor.fetchone()


def sweep_reactive_state(db):
    with db.cursor() as cursor:
        cursor.execute("DELETE FROM llm_reactive_reply_turn WHERE admitted_at < NOW() - INTERVAL 1 DAY LIMIT 1000")
        cursor.execute("DELETE FROM llm_reactive_chat_history WHERE created_at < NOW() - INTERVAL 1 DAY LIMIT 1000")
    db.commit()
