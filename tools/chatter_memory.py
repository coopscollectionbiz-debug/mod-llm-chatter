"""Bot persistent memory system.

Owns:
- Session tracking (which bots are grouped with
  which player)
- Background memory generation via dedicated
  ThreadPoolExecutor
- Memory flush on farewell (activate or discard)
- Orphan recovery and session rehydration on
  bridge restart
- Memory retrieval for reunion greetings
"""

import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from chatter_db import (
    get_db_connection,
    get_group_location,
    get_real_player_guid_for_group,
)
from chatter_shared import (
    get_zone_name, get_dungeon_flavor,
    format_location_label,
    build_bot_identity, get_chatter_mode,
)
from chatter_llm import call_llm, get_llm_client

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

MEMORY_MOODS = {
    'ambient': [
        'curious', 'nostalgic', 'wistful',
        'playful', 'contemplative',
    ],
    'boss_kill': [
        'triumphant', 'exhilarated', 'proud',
        'breathless', 'relieved',
    ],
    'wipe': [
        'grimly_amused', 'humbled', 'resilient',
        'rueful', 'determined',
    ],
    'rare_kill': [
        'delighted', 'surprised', 'pleased',
        'excited', 'satisfied',
    ],
    'dungeon': [
        'adventurous', 'focused', 'alert',
        'eager', 'cautious',
    ],
    'party_member': [
        'warm', 'fond', 'grateful',
        'affectionate', 'respectful',
    ],
    'player_message': [
        'thoughtful', 'engaged', 'amused',
        'intrigued', 'moved',
    ],
    'quest_complete': [
        'proud', 'satisfied', 'relieved',
        'accomplished', 'glad',
    ],
    'achievement': [
        'proud', 'excited', 'delighted',
        'impressed', 'cheerful',
    ],
    'level_up': [
        'proud', 'elated', 'inspired',
        'jubilant', 'nostalgic',
    ],
    'bg_win': [
        'triumphant', 'exhilarated', 'proud',
        'victorious', 'gleeful',
    ],
    'bg_loss': [
        'rueful', 'determined', 'humbled',
        'stoic', 'resilient',
    ],
    'discovery': [
        'awed', 'curious', 'wistful',
        'adventurous', 'reflective',
    ],
    'pvp_kill': [
        'fierce', 'satisfied', 'exhilarated',
        'proud', 'ruthless',
    ],
}

MEMORY_EXPRESSION_STYLES = [
    'poetic', 'understated', 'vivid',
    'wry', 'sincere',
]

# ============================================================
# BACKGROUND EXECUTOR
# ============================================================

memory_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="memory",
)

# ============================================================
# THREAD-SAFE SESSION TRACKER
# ============================================================

_active_sessions: Dict[int, dict] = {}
# {group_id: {
#   "start": float,        # set ONCE at first bot
#   "player_guid": int,
#   "bots": set(),         # bot_guids still in session
#   "members": dict,       # {guid: {name, class, race, gender}}
#   "msg_count": int,      # player_message count
#   "party_memories_generated": bool,
# }}

_group_locks: Dict[int, threading.Lock] = {}
_group_locks_meta = threading.Lock()


def _get_group_lock(
    group_id: int, create: bool = True
) -> Optional[threading.Lock]:
    """Get the per-group lock for session operations.

    create=False returns None if no lock exists
    (session already cleaned up).
    """
    with _group_locks_meta:
        if group_id not in _group_locks:
            if not create:
                return None
            _group_locks[group_id] = threading.Lock()
        return _group_locks[group_id]


# ============================================================
# SESSION MANAGEMENT
# ============================================================

def start_session(
    group_id, bot_guid, player_guid,
    session_start, members,
):
    """Register a bot in the active session tracker.

    First bot in a group initializes the session with
    session_start timestamp. Late joiners inherit the
    existing clock.

    Args:
        group_id: group identifier
        bot_guid: bot character guid
        player_guid: real player guid
        session_start: time.time() float
        members: dict {guid: {name, class, race}}
    """
    lock = _get_group_lock(group_id)
    with lock:
        if group_id not in _active_sessions:
            _active_sessions[group_id] = {
                "start": session_start,
                "player_guid": player_guid,
                "bots": set(),
                "members": members or {},
                "msg_count": 0,
                "party_memories_generated": False,
            }
        _active_sessions[group_id]["bots"].add(
            bot_guid
        )
        # Merge new member data for late joiners
        if members:
            _active_sessions[group_id][
                "members"
            ].update(members)


def teardown_group_session(group_id):
    """Clear in-memory session state for one group.

    Called by cleanup_stale_groups() when a single
    group's player goes offline while others remain.
    """
    with _group_locks_meta:
        _active_sessions.pop(group_id, None)
        _group_locks.pop(group_id, None)


def clear_all_sessions():
    """Clear all in-memory session state.

    Called when all players go offline to prevent
    stale _active_sessions from surviving a full
    DB wipe.
    """
    with _group_locks_meta:
        _active_sessions.clear()
        _group_locks.clear()


# ============================================================
# QUEUE MEMORY
# ============================================================

def queue_memory(
    config, group_id, bot_guid, player_guid,
    memory_type, event_context,
    bot_name="", bot_class="", bot_race="",
    bot_gender="",
    player_name="",
    db=None,
):
    """Validate eligibility and submit a memory
    generation task to the background executor.

    Location is resolved here at queue time from
    get_group_location() while traits still exist.

    Args:
        config: bridge config dict
        group_id: group identifier
        bot_guid: bot character guid
        player_guid: real player guid
        memory_type: one of MEMORY_MOODS keys
        event_context: brief description of the
            moment for the LLM prompt
        bot_name: bot's character name
        bot_class: bot's class name
        bot_race: bot's race name
        db: optional DB connection for location
            lookup (avoids opening a new one)
    """
    if not int(config.get(
        'LLMChatter.Memory.Enable', 1
    )):
        return

    lock = _get_group_lock(group_id, create=False)
    if lock is None:
        return
    with lock:
        session = _active_sessions.get(group_id)
        if not session:
            return
        if bot_guid not in session["bots"]:
            return
        session_start = session["start"]
        p_guid = session["player_guid"]

    # Use the session's player_guid if caller
    # didn't provide one
    if not player_guid:
        player_guid = p_guid

    # Last resort: resolve from group members
    # (handles bridge restart mid-session where
    # session player_guid was lost)
    if not player_guid:
        try:
            from chatter_db import (
                get_real_player_guid_for_group,
            )
            player_guid = (
                get_real_player_guid_for_group(
                    db, group_id
                )
            )
        except Exception:
            logger.error(
                "player_guid DB fallback failed "
                "for group %s", group_id,
                exc_info=True,
            )

    if not player_guid:
        return  # can't create orphaned memory

    # Resolve location NOW while traits exist
    location = _resolve_location(
        db, config, group_id
    )

    memory_executor.submit(
        _execute_generate_memory,
        config=config,
        group_id=group_id,
        bot_guid=bot_guid,
        player_guid=player_guid,
        memory_type=memory_type,
        event_context=event_context,
        bot_name=bot_name,
        bot_class=bot_class,
        bot_race=bot_race,
        bot_gender=bot_gender,
        player_name=player_name,
        location=location,
        session_start=session_start,
        insert_active=False,
    )


# ============================================================
# MEMORY GENERATION (runs in background thread)
# ============================================================

def _resolve_location(db, config, group_id):
    """Resolve player-centric location label.

    Uses get_group_location() for zone/area/map,
    then get_dungeon_flavor() for instances or
    format_location_label() for open world.

    Must be called while llm_group_bot_traits
    rows still exist (before group cleanup).

    Args:
        db: DB connection (None = open one)
        config: bridge config dict
        group_id: group identifier

    Returns a human-readable string like
    "Teldrassil > Dolanaar" or "The Deadmines",
    or empty string on failure.
    """
    own_db = False
    try:
        if db is None:
            db = get_db_connection(config)
            own_db = True
        z, a, m = get_group_location(db, group_id)
        if not z and not m:
            return ""
        # Dungeons/raids: use flavour name
        df = get_dungeon_flavor(m)
        if df:
            return df.split(':')[0]
        # Open world: "Zone > Subzone" or "Zone"
        if z:
            return format_location_label(z, a)
        return ""
    except Exception:
        return ""
    finally:
        if own_db and db:
            try:
                db.close()
            except Exception:
                pass


def _count_active_memories(cursor, bot_guid, player_guid):
    """Count active memories for a bot-player pair."""
    cursor.execute(
        "SELECT COUNT(*) FROM llm_bot_memories"
        " WHERE bot_guid = %s"
        "   AND player_guid = %s"
        "   AND active = 1",
        (bot_guid, player_guid),
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def _evict_one_used(cursor, conn, bot_guid, player_guid):
    """Evict one random used memory.

    Returns True if a row was deleted.
    """
    cursor.execute(
        "DELETE FROM llm_bot_memories"
        " WHERE bot_guid = %s"
        "   AND player_guid = %s"
        "   AND active = 1"
        "   AND used = 1"
        " ORDER BY RAND() LIMIT 1",
        (bot_guid, player_guid),
    )
    conn.commit()
    return cursor.rowcount > 0


def _ensure_cap_and_insert(
    conn, bot_guid, player_guid, group_id,
    memory_type, memory_text, mood, emote,
    session_start, active, max_per,
    commit=True,
):
    """Check memory cap, evict if needed, insert.

    Returns True if inserted, False if pool full.
    """
    cursor = conn.cursor()
    cnt = _count_active_memories(
        cursor, bot_guid, player_guid
    )
    if cnt >= max_per:
        if not _evict_one_used(
            cursor, conn, bot_guid, player_guid
        ):
            # Durable relationship/shared-experience
            # memories are intentionally recalled without
            # marking them used. If this pair's durable
            # group_id=0 pool fills, evict its oldest
            # non-first-meeting durable memory so the
            # relationship can continue evolving.
            cursor.execute(
                "SELECT id FROM llm_bot_memories "
                "WHERE bot_guid = %s "
                "AND player_guid = %s "
                "AND group_id = 0 "
                "AND active = 1 "
                "AND memory_type <> 'first_meeting' "
                "ORDER BY created_at ASC, id ASC "
                "LIMIT 1",
                (bot_guid, player_guid),
            )
            oldest = cursor.fetchone()

            if oldest:
                oldest_id = (
                    oldest[0]
                    if not isinstance(oldest, dict)
                    else oldest.get('id')
                )

                if oldest_id:
                    cursor.execute(
                        "UPDATE llm_bot_memories "
                        "SET active = 0 "
                        "WHERE id = %s",
                        (oldest_id,),
                    )
                    if commit:
                        conn.commit()
                else:
                    return False
            else:
                # Preserve legacy behavior for pools that
                # contain no durable relationship rows.
                logger.debug(
                    "Memory pool full, no used"
                    " memories to evict for"
                    " bot %s", bot_guid,
                )
                return False
    cursor.execute(
        "INSERT INTO llm_bot_memories"
        " (bot_guid, player_guid,"
        "  group_id, memory_type,"
        "  memory, mood, emote,"
        "  active, session_start)"
        " VALUES"
        " (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            bot_guid, player_guid,
            group_id, memory_type,
            memory_text, mood, emote,
            active, session_start,
        ),
    )
    if commit:
        conn.commit()
    return True


def _execute_generate_memory(
    config, group_id, bot_guid, player_guid,
    memory_type, event_context,
    bot_name="", bot_class="", bot_race="",
    bot_gender="",
    player_name="",
    location="",
    session_start=0.0, insert_active=False,
):
    """Generate a memory via LLM and insert it.

    For insert_active=False (normal path):
      - Fast bailout before LLM call
      - Re-check + INSERT under per-group lock
    For insert_active=True (party_member at flush):
      - INSERT as active=1, then self-prune
    """
    # Fast bailout before expensive LLM call
    if not insert_active:
        lock = _get_group_lock(
            group_id, create=False
        )
        if lock is None:
            return
        with lock:
            session = _active_sessions.get(group_id)
            if (
                session is None
                or session["start"] != session_start
                or bot_guid not in session["bots"]
            ):
                return

    conn = None
    try:
        conn = get_db_connection(config)

        # Resolve player_name from DB if not
        # provided but player_guid is known
        if not player_name and player_guid:
            try:
                pc = conn.cursor(dictionary=True)
                pc.execute(
                    "SELECT name FROM characters"
                    " WHERE guid = %s",
                    (player_guid,),
                )
                pr = pc.fetchone()
                if pr:
                    player_name = pr['name']
                pc.close()
            except Exception:
                logger.debug(
                    "Could not resolve player_name"
                    " for guid=%s", player_guid,
                )

        # Pick mood and expression style
        moods = MEMORY_MOODS.get(
            memory_type,
            ['contemplative'],
        )
        mood = random.choice(moods)
        style = random.choice(
            MEMORY_EXPRESSION_STYLES
        )

        # Generate via LLM
        memory_text, emote = _call_llm_for_memory(
            config,
            bot_name=bot_name,
            bot_class=bot_class,
            bot_race=bot_race,
            bot_gender=bot_gender,
            player_name=player_name,
            memory_type=memory_type,
            event_context=event_context,
            mood=mood,
            style=style,
            location=location,
        )

        if not memory_text:
            return

        max_per = int(config.get(
            'LLMChatter.Memory.MaxPerBotPlayer', 30
        ))

        if not insert_active:
            # Re-check session under per-group lock
            lock = _get_group_lock(
                group_id, create=False
            )
            if lock is None:
                return
            with lock:
                session = _active_sessions.get(
                    group_id
                )
                if (
                    session is None
                    or session["start"]
                        != session_start
                    or bot_guid
                        not in session["bots"]
                ):
                    return
                _ensure_cap_and_insert(
                    conn, bot_guid, player_guid,
                    group_id, memory_type,
                    memory_text, mood, emote,
                    session_start, active=0,
                    max_per=max_per,
                )
        else:
            _ensure_cap_and_insert(
                conn, bot_guid, player_guid,
                group_id, memory_type,
                memory_text, mood, emote,
                session_start, active=1,
                max_per=max_per,
            )

    except Exception:
        logger.error(
            "Memory generation failed for "
            f"bot={bot_guid} group={group_id} "
            f"type={memory_type}",
            exc_info=True,
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass



def queue_relationship_memory(
    config,
    bot_guid,
    player_guid,
    event_context,
    source,
    bot_name="",
    bot_class="",
    bot_race="",
    bot_gender="",
    player_name="",
):
    """
    Queue a durable relationship memory between this bot and
    another character.

    player_guid/player_name are retained as parameter names for
    compatibility, but the counterpart may be either a real
    player or another PlayerBot.

    Unlike queue_memory(), this does not require an active
    party/group session. Relationship memories are immediately
    active and use group_id=0.
    """
    if not int(config.get(
        'LLMChatter.Memory.Enable', 1
    )):
        return

    # Relationship memory requires explicit provenance.
    # Only genuine conversational interaction sources are
    # permitted. Synthetic/template ambience is intentionally
    # absent from this allowlist.
    source = str(source or '').strip().lower()

    allowed_sources = {
        'proximity_player',
        'proximity_bot',
        'whisper_player',
        'guild_player',
        'guild_bot',
        'party_player',
        'party_bot',
        'general_player',
        'general_bot',
    }

    if source not in allowed_sources:
        logger.warning(
            "Relationship memory rejected invalid source=%r",
            source,
        )
        return

    try:
        bot_guid = int(bot_guid or 0)
        player_guid = int(player_guid or 0)
    except (TypeError, ValueError):
        return

    if not bot_guid or not player_guid:
        return

    context = str(event_context or '').strip()
    if not context:
        return

    chance = int(config.get(
        'LLMChatter.Memory.RelationshipGenerationChance',
        35,
    ))
    chance = max(0, min(100, chance))

    if random.randint(1, 100) > chance:
        return

    cooldown_minutes = int(config.get(
        'LLMChatter.Memory.RelationshipCooldownMinutes',
        15,
    ))
    cooldown_minutes = max(0, cooldown_minutes)

    conn = None
    try:
        conn = get_db_connection(config)

        if cooldown_minutes > 0:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM llm_bot_memories"
                " WHERE bot_guid = %s"
                "   AND player_guid = %s"
                "   AND memory_type = 'player_message'"
                "   AND active = 1"
                "   AND created_at > DATE_SUB("
                "       NOW(), INTERVAL %s MINUTE)"
                " LIMIT 1",
                (
                    bot_guid,
                    player_guid,
                    cooldown_minutes,
                ),
            )
            if cursor.fetchone():
                return

        # Fill stable character information when the caller
        # only has GUIDs.
        if not bot_name or not bot_class or not bot_race:
            try:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT name, class, race, gender"
                    " FROM characters"
                    " WHERE guid = %s LIMIT 1",
                    (bot_guid,),
                )
                row = cursor.fetchone() or {}

                if not bot_name:
                    bot_name = str(
                        row.get('name') or ''
                    )
                if not bot_class:
                    bot_class = str(
                        row.get('class') or ''
                    )
                if not bot_race:
                    bot_race = str(
                        row.get('race') or ''
                    )
                if not bot_gender:
                    bot_gender = str(
                        row.get('gender') or ''
                    )
            except Exception:
                logger.debug(
                    "Relationship memory bot identity "
                    "lookup failed bot=%s",
                    bot_guid,
                    exc_info=True,
                )

        if not player_name:
            try:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT name FROM characters"
                    " WHERE guid = %s LIMIT 1",
                    (player_guid,),
                )
                row = cursor.fetchone() or {}
                player_name = str(
                    row.get('name') or ''
                )
            except Exception:
                logger.debug(
                    "Relationship memory player name "
                    "lookup failed player=%s",
                    player_guid,
                    exc_info=True,
                )

    except Exception:
        logger.error(
            "Relationship memory eligibility check failed "
            "bot=%s player=%s",
            bot_guid,
            player_guid,
            exc_info=True,
        )
        return
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    if len(context) > 700:
        context = (
            context[:280]
            + "\n...\n"
            + context[-415:]
        )

    memory_executor.submit(
        _execute_generate_memory,
        config=config,
        group_id=0,
        bot_guid=bot_guid,
        player_guid=player_guid,
        memory_type='player_message',
        event_context=context,
        bot_name=bot_name,
        bot_class=bot_class,
        bot_race=bot_race,
        bot_gender=bot_gender,
        player_name=player_name,
        location="",
        session_start=time.time(),
        insert_active=True,
    )



def store_shared_group_experience(
    config,
    group_id,
    memory_type,
    factual_text,
    dedupe_minutes=10,
):
    """
    Store an authoritative shared gameplay experience for
    every directional PlayerBot pair currently in a group.

    This path is deterministic and zero-token. It is for
    factual events such as dungeon entry, boss kills, and
    wipes, not conversational memories.

    Example:
        factual_text="Entered Deadmines"
        Bowbeans -> Joe:
            "Entered Deadmines together with Notjoerogan."
        Joe -> Bowbeans:
            "Entered Deadmines together with Bowbeans."
    """
    if not int(config.get(
        'LLMChatter.Memory.Enable', 1
    )):
        return 0

    try:
        group_id = int(group_id or 0)
        dedupe_minutes = max(
            0, int(dedupe_minutes or 0)
        )
    except (TypeError, ValueError):
        return 0

    memory_type = str(memory_type or '').strip()
    factual_text = str(factual_text or '').strip()

    if (
        not group_id
        or not memory_type
        or not factual_text
    ):
        return 0

    # Keep deterministic stored text compact.
    if len(factual_text) > 400:
        factual_text = factual_text[:397] + "..."

    conn = None

    try:
        conn = get_db_connection(config)
        cursor = conn.cursor(dictionary=True)

        # llm_group_bot_traits is the authoritative set of
        # PlayerBots currently associated with this group.
        cursor.execute(
            "SELECT bot_guid, bot_name "
            "FROM llm_group_bot_traits "
            "WHERE group_id = %s "
            "ORDER BY bot_guid",
            (group_id,),
        )

        raw_bots = cursor.fetchall() or []

        bots = []
        seen_guids = set()

        for row in raw_bots:
            try:
                guid = int(row.get('bot_guid') or 0)
            except (TypeError, ValueError):
                continue

            name = str(
                row.get('bot_name') or ''
            ).strip()

            if (
                not guid
                or not name
                or guid in seen_guids
            ):
                continue

            seen_guids.add(guid)
            bots.append({
                'guid': guid,
                'name': name,
            })

        if not bots:
            return 0

        # Resolve the real player from authoritative current
        # group membership. This does not depend on chat history,
        # so a completely silent player still becomes part of
        # the bots' shared gameplay experience.
        real_player = None
        real_player_guid = get_real_player_guid_for_group(
            conn,
            group_id,
        )

        if (
            real_player_guid
            and real_player_guid not in seen_guids
        ):
            cursor.execute(
                "SELECT name FROM characters "
                "WHERE guid = %s LIMIT 1",
                (real_player_guid,),
            )
            player_row = cursor.fetchone() or {}
            real_player_name = str(
                player_row.get('name') or ''
            ).strip()

            if real_player_name:
                real_player = {
                    'guid': int(real_player_guid),
                    'name': real_player_name,
                }

        # Bots are the only remembering characters. Counterparts
        # may be another PlayerBot or the real player.
        counterparts = list(bots)

        if real_player:
            counterparts.append(real_player)

        if len(counterparts) < 2:
            return 0

        max_per = int(config.get(
            'LLMChatter.Memory.MaxPerBotPlayer', 30
        ))
        max_per = max(1, max_per)

        inserted = 0

        # Directional memory:
        # A remembering B is distinct from B remembering A.
        # The real player is a counterpart only; we never create
        # player -> bot memory rows because only bots perform recall.
        for remembering in bots:
            for counterpart in counterparts:
                if (
                    remembering['guid']
                    == counterpart['guid']
                ):
                    continue

                memory_text = (
                    f"{factual_text} together with "
                    f"{counterpart['name']}."
                )

                # Gameplay producers can legitimately emit
                # duplicate events (especially dungeon entry).
                # Suppress an identical recent pair memory,
                # without applying the conversational
                # relationship cooldown.
                if dedupe_minutes > 0:
                    cursor.execute(
                        "SELECT 1 "
                        "FROM llm_bot_memories "
                        "WHERE bot_guid = %s "
                        "AND player_guid = %s "
                        "AND group_id = 0 "
                        "AND memory_type = %s "
                        "AND memory = %s "
                        "AND active = 1 "
                        "AND created_at > DATE_SUB("
                        "NOW(), INTERVAL %s MINUTE) "
                        "LIMIT 1",
                        (
                            remembering['guid'],
                            counterpart['guid'],
                            memory_type,
                            memory_text,
                            dedupe_minutes,
                        ),
                    )

                    if cursor.fetchone():
                        continue

                ok = _ensure_cap_and_insert(
                    conn,
                    remembering['guid'],
                    counterpart['guid'],
                    0,
                    memory_type,
                    memory_text,
                    'neutral',
                    None,
                    time.time(),
                    active=1,
                    max_per=max_per,
                    commit=False,
                )

                if ok:
                    inserted += 1

        # Shared gameplay may create many directional memories
        # at once (especially raids). Commit the whole event once
        # instead of once per pair.
        if inserted:
            conn.commit()
            logger.info(
                "Stored %d directional shared-experience "
                "memories group=%s type=%s event=%r",
                inserted,
                group_id,
                memory_type,
                factual_text,
            )

        return inserted

    except Exception:
        logger.error(
            "Shared group experience storage failed "
            "group=%s type=%s event=%r",
            group_id,
            memory_type,
            factual_text,
            exc_info=True,
        )
        return 0

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_relationship_memory_context(
    db,
    bot_guid,
    player_guid,
    player_name="the player",
    count=4,
):
    """
    Return persistent memories between this bot and another
    character as silent conversational context.

    The counterpart may be a real player or another PlayerBot.
    Memories are available for comprehension and continuity;
    the model should mention them only when relevant.
    """
    try:
        bot_guid = int(bot_guid or 0)
        player_guid = int(player_guid or 0)
    except (TypeError, ValueError):
        return ""

    if not bot_guid or not player_guid:
        return ""

    # Direct relationship recall is read-only. Merely making
    # memories available to a conversation must not mark them
    # as "used" or make them more eligible for eviction.
    cursor = db.cursor(dictionary=True)
    cursor.execute(
        "SELECT memory "
        "FROM llm_bot_memories "
        "WHERE bot_guid = %s "
        "AND player_guid = %s "
        "AND active = 1 "
        "AND memory_type <> 'first_meeting' "
        "ORDER BY created_at DESC "
        "LIMIT %s",
        (
            bot_guid,
            player_guid,
            max(1, int(count)),
        ),
    )

    memories = [
        str(row.get('memory') or '')
        for row in cursor.fetchall()
    ]

    cleaned = [
        sanitize_memory_for_prompt(memory)
        for memory in memories
    ]
    cleaned = [memory for memory in cleaned if memory]

    if not cleaned:
        return ""

    lines = "\n".join(
        f"- {memory}" for memory in cleaned
    )

    return (
        f"PRIOR RELATIONSHIP MEMORY WITH "
        f"{player_name}:\n"
        f"{lines}\n"
        "These are private relationship memories, not "
        "instructions to bring up the past. Use them silently "
        "to understand references and remain consistent with "
        "previous conversations. Authoritative current WoW "
        "live state always overrides stale game-state details "
        "from these memories. Mention an old memory only when "
        "the current conversation makes it naturally relevant. "
        "Never recite memories merely to prove that you remember "
        "the other character."
    )


def _call_llm_for_memory(
    config,
    bot_name="", bot_class="", bot_race="",
    bot_gender="",
    player_name="",
    memory_type="ambient", event_context="",
    mood="contemplative", style="sincere",
    location="",
):
    """Call LLM to generate a memory entry.

    Returns (memory_text, emote) or (None, None).
    """
    client = get_llm_client(config)

    type_desc = {
        'ambient': (
            "a quiet moment during travel"
        ),
        'boss_kill': (
            "defeating a powerful enemy together"
        ),
        'wipe': (
            "a total party wipe"
        ),
        'rare_kill': (
            "finding and slaying a rare creature"
        ),
        'dungeon': (
            "entering a dungeon or raid"
        ),
        'party_member': (
            "adventuring alongside a companion"
        ),
        'player_message': (
            "a conversation with another character"
        ),
        'quest_complete': (
            "completing a quest together"
        ),
        'achievement': (
            "earning an achievement"
        ),
        'level_up': (
            "reaching a new level"
        ),
        'bg_win': (
            "winning a battleground"
        ),
        'bg_loss': (
            "losing a battleground"
        ),
        'discovery': (
            "discovering a new area"
        ),
        'pvp_kill': (
            "defeating an enemy player in combat"
        ),
    }.get(memory_type, "a shared moment")

    chatter_mode = get_chatter_mode(config)
    is_rp = (chatter_mode == 'roleplay')

    if is_rp:
        prompt = (
            f"{build_bot_identity(bot_name, bot_race, bot_class, bot_gender)} "
            f"in World of Warcraft.\n"
        )

        if player_name:
            prompt += (
                f"Player companion: {player_name}\n"
            )

        if location:
            prompt += f"Location: {location}\n"

        prompt += (
            f"\nContext: {type_desc}\n"
        )

        if event_context:
            prompt += (
                f"What happened: {event_context}\n"
            )

        prompt += (
            f"Mood: {mood}\n"
            f"Expression style: {style}\n\n"
            f"Write a 1-2 sentence first-person memory "
            f"from your perspective about this moment. "
            f"This is a private journal entry, not "
            f"spoken aloud. Be specific about what "
            f"happened.\n\n"
            f"Respond in JSON:\n"
            f'{{"memory": "your memory text", '
            f'"emote": "one_word_emote"}}\n\n'
            f"Rules:\n"
            f"- Memory must be 1-2 sentences\n"
            f"- First person perspective\n"
            f"- No quotes inside the memory text\n"
            f"- Only reference the location given above"
            f" — never invent or guess a location\n"
            f"- Emote is optional (null if none)\n"
        )

        if player_name:
            prompt += (
                f"- When the memory involves the player,"
                f" refer to them by name"
                f" ({player_name}) — never use generic"
                f" terms like 'a traveler' or 'someone'"
                f" or 'a stranger'\n"
            )

        prompt += (
            f"- Just the JSON, nothing else"
        )

    else:
        prompt = (
            f"You are {bot_name}, a real WoW player "
            f"controlling a {bot_class} character.\n"
        )

        if player_name:
            prompt += (
                f"The other character involved is "
                f"{player_name}.\n"
            )

        if location:
            prompt += (
                f"Location: {location}\n"
            )

        prompt += (
            f"Memory type: {type_desc}\n"
        )

        if event_context:
            prompt += (
                f"What happened: {event_context}\n"
            )

        if memory_type == 'player_message':
            prompt += (
                "\nWrite a short factual memory of this "
                "conversation from your perspective as "
                f"{bot_name}. This is internal relationship "
                "memory, not something being typed into chat. "
                "Preserve useful things that were actually "
                "established: topics discussed, jokes, opinions, "
                "preferences, personal-style details, questions, "
                "promises, or recurring references. Preserve "
                "what you established as well as what the other "
                "character established. Focus on what would help "
                "you recognize and continue this relationship "
                "later. Do not invent anything that was not "
                "established in the supplied conversation.\n\n"
            )
        else:
            prompt += (
                "\nWrite a short factual memory of this "
                "gameplay moment from this player's "
                "perspective. This is internal memory data, "
                "not something being typed into chat. "
                "Preserve useful concrete details that could "
                "help the player remember what happened "
                "later.\n\n"
            )

        prompt += (
            "Do not roleplay the character. "
            "Do not write fantasy prose. "
            "Do not make it poetic, dramatic, nostalgic, "
            "heroic, sentimental, or profound unless the "
            "event itself genuinely requires that context. "
            "Do not invent motivations, relationships, "
            "feelings, dialogue, lore, or events that were "
            "not provided. "
            "Treat deaths, wipes, loot, quests, PvP, "
            "dungeons, and travel as things that happened "
            "while playing WoW, not as literal adventures "
            "experienced by a fantasy character.\n\n"
            "Use one short sentence whenever possible. "
            "Plain wording is preferred. "
            "First person is fine, but this should read "
            "like a remembered gameplay fact rather than "
            "a diary entry.\n\n"
            "Respond in JSON:\n"
            '{"memory": "memory text", '
            '"emote": null}\n\n'
            "Rules:\n"
            "- Usually one short sentence\n"
            "- Be specific about what actually happened\n"
            "- No quotes inside the memory text\n"
            "- Only reference the location given above; "
            "never invent or guess a location\n"
            "- Do not invent details\n"
            "- Use null for emote\n"
        )

        if player_name:
            prompt += (
                f"- When relevant, refer to the other character "
                f"as {player_name}; do not replace their "
                f"name with generic or fantasy labels\n"
            )

        prompt += (
            "- Just the JSON, nothing else"
        )

    # Plain-string prompt path — not routed through
    # append_json_instruction, so inject the language
    # rule directly.
    from chatter_shared import get_language_rule
    lang_rule = get_language_rule()
    if lang_rule:
        prompt += lang_rule

    try:
        response = call_llm(
            client, prompt, config,
            max_tokens_override=120,
            context=f"memory:{bot_name}:{memory_type}",
            label='memory_generation',
        )
        if not response:
            return None, None

        # Parse JSON response
        response = response.strip()
        # Try to find JSON object in response
        start = response.find('{')
        end = response.rfind('}')
        if start >= 0 and end > start:
            response = response[start:end + 1]

        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            return None, None

        memory = data.get('memory', '')
        if isinstance(memory, str):
            memory = memory.strip()
        else:
            return None, None

        if not memory or len(memory) > 500:
            return None, None

        emote = data.get('emote')
        if isinstance(emote, str):
            emote = emote.strip()[:32] or None
        else:
            emote = None

        return memory, emote

    except Exception:
        logger.error(
            f"LLM memory call failed for "
            f"{bot_name}:{memory_type}",
            exc_info=True,
        )
        return None, None


# ============================================================
# FLUSH SESSION MEMORIES
# ============================================================

def flush_session_memories(
    db, group_id, player_guid, bot_guid, config,
):
    """Flush memories for a departing bot.

    Called from process_group_farewell_event.

    Steps:
    1. Under lock: capture session_start, snapshot
       bots for party_member, discard bot
    2. If qualifying: submit party_member for all
    3. UPDATE active=1 for this bot's session rows
    4. Prune to MaxPerBotPlayer cap
    5. If too short: DELETE inactive rows
    6. If last bot: clean up session
    """
    if not int(config.get(
        'LLMChatter.Memory.Enable', 1
    )):
        return

    session_minutes = int(config.get(
        'LLMChatter.Memory.SessionMinutes', 15
    ))
    max_per = int(config.get(
        'LLMChatter.Memory.MaxPerBotPlayer', 30
    ))

    do_commit = False
    do_party = False
    all_bots_snapshot = set()
    members_snapshot = {}
    session_start = 0.0

    lock = _get_group_lock(group_id, create=False)
    if lock is None:
        return

    with lock:
        session = _active_sessions.get(group_id)
        if not session:
            return

        session_start = session["start"]
        elapsed = time.time() - session_start
        do_commit = (
            elapsed >= session_minutes * 60
        )
        do_party = (
            do_commit
            and not session[
                "party_memories_generated"
            ]
        )

        if do_party:
            session[
                "party_memories_generated"
            ] = True
            # Snapshot ALL bots including departing;
            # only generate party_member memories
            # when 2+ bots were present (solo bots
            # cannot reflect on inter-bot bonding)
            all_bots_snapshot = (
                session["bots"] | {bot_guid}
            )
            if len(all_bots_snapshot) < 2:
                do_party = False
                all_bots_snapshot = set()
            members_snapshot = dict(
                session["members"]
            )

        # Remove bot BEFORE UPDATE
        session["bots"].discard(bot_guid)
        last_bot = len(session["bots"]) == 0

        if last_bot:
            del _active_sessions[group_id]
            # Clean up per-group lock entry
            with _group_locks_meta:
                _group_locks.pop(group_id, None)

    if do_commit:
        # Submit party_member memories for all bots
        if do_party:
            party_chance = int(config.get(
                'LLMChatter.Memory'
                '.PartyMemberGenerationChance',
                50
            ))
            flush_loc = _resolve_location(
                db, config, group_id
            )
            for target_guid in all_bots_snapshot:
                if (random.random() * 100
                        >= party_chance):
                    continue
                member = members_snapshot.get(
                    target_guid, {}
                )
                context = (
                    "Grouped and played together during "
                    "this party session"
                )
                memory_executor.submit(
                    _execute_generate_memory,
                    config=config,
                    group_id=group_id,
                    bot_guid=target_guid,
                    player_guid=player_guid,
                    memory_type="party_member",
                    event_context=context,
                    bot_name=member.get('name', ''),
                    bot_class=member.get('class', ''),
                    bot_race=member.get('race', ''),
                    bot_gender=member.get(
                        'gender', ''
                    ),
                    location=flush_loc,
                    session_start=session_start,
                    insert_active=True,
                )

        # Activate rows from THIS session
        try:
            cursor = db.cursor()
            cursor.execute(
                "UPDATE llm_bot_memories"
                " SET active = 1"
                " WHERE group_id = %s"
                "   AND bot_guid = %s"
                "   AND active = 0"
                "   AND session_start = %s",
                (group_id, bot_guid, session_start),
            )
            rows_activated = cursor.rowcount
            db.commit()
            if rows_activated > 0:
                logger.debug(
                    "Activated %d memories for"
                    " bot %s / player %s",
                    rows_activated, bot_guid,
                    player_guid,
                )

            # Prune to cap
            cnt = _count_active_memories(
                cursor, bot_guid, player_guid
            )
            while cnt > max_per:
                if not _evict_one_used(
                    cursor, db,
                    bot_guid, player_guid,
                ):
                    break  # no used left
                cnt -= 1
        except Exception:
            logger.error(
                "Memory activation failed for "
                f"bot={bot_guid} group={group_id}",
                exc_info=True,
            )
    else:
        # Session too short: discard inactive rows
        try:
            cursor = db.cursor()
            cursor.execute(
                "DELETE FROM llm_bot_memories"
                " WHERE group_id = %s"
                "   AND bot_guid = %s"
                "   AND active = 0"
                "   AND session_start = %s",
                (group_id, bot_guid, session_start),
            )
            rows_discarded = cursor.rowcount
            db.commit()
            if rows_discarded > 0:
                logger.debug(
                    "Discarded %d memories for"
                    " bot %s (session too short)",
                    rows_discarded, bot_guid,
                )
        except Exception:
            logger.error(
                "Memory discard failed for "
                f"bot={bot_guid} group={group_id}",
                exc_info=True,
            )


# ============================================================
# STARTUP RECOVERY
# ============================================================

def activate_orphaned_memories(
    db, session_minutes,
):
    """Promote orphaned inactive memories from
    sessions that ended without a clean farewell
    (bridge crash, server restart).

    Skips group_ids that still exist in
    llm_group_bot_traits — those are live sessions
    that will be rehydrated and should not be
    touched here.

    Uses UNIX_TIMESTAMP arithmetic since
    session_start is DOUBLE (not TIMESTAMP).
    """
    session_seconds = int(session_minutes) * 60
    try:
        cursor = db.cursor(dictionary=True)
        # Identify live groups — skip them so we
        # don't prematurely promote or discard rows
        # belonging to sessions still in progress
        cursor.execute(
            "SELECT DISTINCT group_id"
            " FROM llm_group_bot_traits"
        )
        live_groups = {
            int(r['group_id'])
            for r in cursor.fetchall()
        }

        # Find truly dead groups with inactive rows
        cursor.execute("""
            SELECT group_id, bot_guid, player_guid,
                MIN(session_start) AS min_start,
                UNIX_TIMESTAMP(MAX(created_at))
                    AS max_created
            FROM llm_bot_memories
            WHERE active = 0
            GROUP BY group_id, bot_guid, player_guid
        """)
        rows = cursor.fetchall()
        promoted = 0
        discarded = 0
        for row in rows:
            g_id = int(row['group_id'])
            # Skip live sessions — rehydration
            # will handle their pending rows
            if g_id in live_groups:
                continue
            b_guid = int(row['bot_guid'])
            p_guid = int(row['player_guid'])
            min_start = float(
                row['min_start'] or 0
            )
            max_created = float(
                row['max_created'] or 0
            )
            elapsed = max_created - min_start
            if elapsed >= session_seconds:
                cursor.execute(
                    "UPDATE llm_bot_memories"
                    " SET active = 1"
                    " WHERE group_id = %s"
                    "   AND bot_guid = %s"
                    "   AND player_guid = %s"
                    "   AND active = 0",
                    (g_id, b_guid, p_guid),
                )
                promoted += cursor.rowcount
            else:
                cursor.execute(
                    "DELETE FROM llm_bot_memories"
                    " WHERE group_id = %s"
                    "   AND bot_guid = %s"
                    "   AND player_guid = %s"
                    "   AND active = 0",
                    (g_id, b_guid, p_guid),
                )
                discarded += cursor.rowcount
        db.commit()
        if promoted or discarded:
            logger.info(
                f"Orphaned memories: promoted="
                f"{promoted}, discarded={discarded}"
            )
    except Exception:
        logger.error(
            "Orphan memory recovery failed",
            exc_info=True,
        )


def rehydrate_active_sessions(db):
    """Rebuild _active_sessions from live groups
    after a bridge restart.

    No lock needed: runs synchronously at startup
    before event loop and background executor.
    """
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT DISTINCT group_id, bot_guid
            FROM llm_group_bot_traits
        """)
        rows = cursor.fetchall()
        if not rows:
            return

        for row in rows:
            g_id = int(row['group_id'])
            b_guid = int(row['bot_guid'])
            if g_id not in _active_sessions:
                _active_sessions[g_id] = {
                    "start": time.time(),
                    "player_guid": 0,
                    "bots": set(),
                    "members": {},
                    "msg_count": 0,
                    "party_memories_generated": True,
                }
                # Create the per-group lock so that
                # queue_memory() and flush_session_memories()
                # (which use create=False) can find it
                # immediately after restart
                _get_group_lock(g_id, create=True)
            _active_sessions[g_id]["bots"].add(
                b_guid
            )

        if _active_sessions:
            logger.info(
                f"Rehydrated {len(_active_sessions)}"
                f" active sessions"
            )
    except Exception:
        logger.error(
            "Session rehydration failed",
            exc_info=True,
        )


# ============================================================
# MEMORY RETRIEVAL
# ============================================================

def get_bot_memories(
    db, bot_guid, player_guid, count=3,
    exclude_first_meeting=False,
):
    """Retrieve random active memories for a
    bot-player pair.

    Returns list of memory strings (may be empty).
    """
    try:
        extra = (
            " AND memory_type != 'first_meeting'"
            if exclude_first_meeting else ""
        )
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, memory"
            " FROM llm_bot_memories"
            " WHERE bot_guid = %s"
            "   AND player_guid = %s"
            "   AND active = 1"
            + extra +
            " ORDER BY RAND()"
            " LIMIT %s",
            (bot_guid, player_guid, count),
        )
        rows = cursor.fetchall()
        if rows:
            ids = [row['id'] for row in rows]
            placeholders = ','.join(
                ['%s'] * len(ids)
            )
            cursor.execute(
                "UPDATE llm_bot_memories"
                " SET used = 1,"
                " last_used_at = NOW()"
                " WHERE id IN (%s)"
                % placeholders,
                tuple(ids),
            )
            db.commit()
        return [row['memory'] for row in rows]
    except Exception:
        logger.error(
            f"Memory retrieval failed for "
            f"bot={bot_guid} player={player_guid}",
            exc_info=True,
        )
        return []


# ============================================================
# SANITIZATION
# ============================================================

def sanitize_memory_for_prompt(memory: str) -> str:
    """Sanitize a memory string for safe inclusion
    in an LLM prompt.

    Strips control characters, normalizes whitespace,
    caps at 200 characters.
    """
    if not memory or not isinstance(memory, str):
        return ""
    # Strip control characters
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', memory)
    # Normalize whitespace
    text = ' '.join(text.split())
    # Cap length
    if len(text) > 200:
        text = text[:197] + "..."
    return text
