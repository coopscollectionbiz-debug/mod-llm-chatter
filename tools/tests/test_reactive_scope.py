"""Synthetic cutover and scoped reactive chat regression cases."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

import test_normal_mode_player_chat  # noqa: F401 - optional SDK stubs
import chatter_general as general
import chatter_group as group
import chatter_group_general_reaction as relay
from chatter_reactive import recent_transcript, reserve_party_reply, select_party_responder


def test_relay_is_disabled_even_with_inherited_enable():
    config = {"LLMChatter.GroupChatter.GeneralRelayEnable": "1"}
    with patch.object(relay, "mark_event") as mark:
        assert not relay.maybe_queue_group_general_reaction(
            None, config, 21, "Flint", "patrol loops at the bridge", 12, source_event_id=101,
        )
        assert not relay.process_group_general_reaction_event(None, None, config, {"id": 102})
        mark.assert_called_once_with(None, 102, "skipped")


def test_general_replies_with_a_shared_party_bot_stay_general():
    event = {
        "id": 101, "subject_guid": 11, "map_id": 0,
        "extra_data": json.dumps({
            "player_name": "Nell", "player_message": "Flint, where does the patrol turn?",
            "bot_guids": [21], "bot_names": ["Flint"], "zone_id": 12,
            "reactive_audience": "general:1:0:0:12:General - Elwynn Forest",
            "reactive_turn": "1:101",
        }),
    }
    primary = dict(bot1_guid=21, bot1_idx=0, bot1_name="Flint", bot1_race="Human",
                   bot1_class="Mage", bot1_class_id=8, bot1_level=24, bot1_gender="male",
                   bot1_traits=[], is_conversation=False, brief_casual=False, reply_optional=False)
    with ExitStack() as stack:
        for name, value in {
            "_get_bot_info": {"race": 1}, "_filter_player_general_candidates": ([21], ["Flint"]),
            "recent_transcript": [], "_select_primary_bot": primary, "willing_to_speak": True,
            "resolve_and_format_links": ("Flint, where does the patrol turn?", ""),
            "call_llm": '{"message":"at the bridge"}',
        }.items():
            stack.enter_context(patch.object(general, name, return_value=value))
        queue = stack.enter_context(patch.object(general, "insert_chat_message"))
        history = stack.enter_context(patch.object(general, "_store_general_chat"))
        party_history = stack.enter_context(patch.object(group, "_store_chat"))
        stack.enter_context(patch.object(general, "mark_event"))
        assert general.process_general_player_msg_event(
            event, None, None, {"LLMChatter.TalentInjectionChance": 0},
        )
    queue.assert_called_once()
    assert queue.call_args.args[1] == 21
    assert queue.call_args.kwargs["channel"] == "general"
    assert queue.call_args.kwargs["event_id"] == 101
    history.assert_not_called()
    party_history.assert_not_called()


def test_responder_prefers_address_then_relevance_and_recent_participation():
    tank = dict(bot_guid=20, bot_name="Flint", role="tank", last_reply=0)
    healer = dict(bot_guid=21, bot_name="Ash", role="healer", last_reply=5)
    assert select_party_responder("Ash, ready?", [tank, healer]) == (healer, True)
    assert select_party_responder("mana for next pull?", [tank, healer]) == (tank, False)
    assert select_party_responder("anyone need mana?", [tank, healer]) == (healer, False)
    assert select_party_responder("left side?", [tank, healer]) == (tank, False)
    assert select_party_responder("ok ty", [tank, healer]) is None
    assert select_party_responder("Nell, pull?", [tank, healer], ["Nell"]) is None
    assert group._is_playerbot_command("@follow")


def party_event(message):
    return {
        "id": 104, "subject_guid": 11,
        "extra_data": json.dumps({
            "group_id": 7, "player_name": "Nell", "player_message": message,
            "reactive_audience": "party:1:7:3:0:0:11,20,21,", "reactive_turn": "1:104",
            "reactive_bots": [20, 21], "reactive_humans": [],
        }),
    }


def test_party_handler_calls_only_the_addressed_bot_and_defers_transcript_to_delivery():
    db = MagicMock()
    cursor = db.cursor.return_value
    cursor.fetchall.return_value = [
        dict(bot_guid=guid, bot_name=name, role=role, faction_race=1, trait1="", trait2="", trait3="")
        for guid, name, role in ((20, "Flint", "tank"), (21, "Ash", "healer"))
    ]
    cursor.fetchone.side_effect = [{"race": 1}, {"class": 5, "race": 1, "gender": 0, "level": 24}]
    with ExitStack() as stack:
        for name, value in {
            "recent_transcript": [], "get_group_location": (12, 12, 0),
            "resolve_and_format_links": ("Ash, ready to pull?", ""),
            "get_character_info_by_name": None, "_maybe_talent_context": None,
            "reserve_party_reply": nullcontext(True),
        }.items():
            stack.enter_context(patch.object(group, name, return_value=value))
        call = stack.enter_context(patch.object(group, "call_llm", return_value='{"message":"mana sec"}'))
        queue = stack.enter_context(patch.object(group, "insert_chat_message"))
        history = stack.enter_context(patch.object(group, "_store_chat"))
        stack.enter_context(patch.object(group, "_mark_event"))
        assert group.process_group_player_msg_event(
            db, None, {"LLMChatter.Memory.Enable": 0}, party_event("Ash, ready to pull?"),
        )
    call.assert_called_once()
    queue.assert_called_once()
    assert queue.call_args.args[1:4] == (21, "Ash", "mana sec")
    assert queue.call_args.kwargs["group_id"] == 7
    history.assert_not_called()


def test_native_at_command_is_rejected_before_party_model_or_db_work():
    with patch.object(group, "_mark_event") as mark, patch.object(group, "call_llm") as model:
        assert not group.process_group_player_msg_event(None, None, {}, party_event("@follow"))
        mark.assert_called_once_with(None, 104, "skipped")
        model.assert_not_called()


@pytest.fixture
def mysql_fixture():
    if os.environ.get("CHATTER_TEST_MYSQL_PORT") != "3337":
        pytest.skip("disposable MySQL on loopback port 3337 required")
    import mysql.connector

    def connect(database=None):
        return mysql.connector.connect(host="127.0.0.1", port=3337, user="root", database=database)

    schema = "chatter_patch1_fixture"
    with connect() as db, db.cursor() as cursor:
        cursor.execute("CREATE DATABASE IF NOT EXISTS chatter_patch1_fixture")
    with connect(schema) as db, db.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS llm_chatter_events, llm_chatter_messages, "
                       "llm_reactive_reply_turn, llm_reactive_chat_history")
        cursor.execute("CREATE TABLE llm_chatter_events "
                       "(id INT PRIMARY KEY, status VARCHAR(12), expires_at DATETIME(6))")
        cursor.execute("CREATE TABLE llm_chatter_messages "
                       "(event_id INT, delivered TINYINT, channel VARCHAR(12))")
        cursor.execute("INSERT INTO llm_chatter_events VALUES "
                       "(101,'processing',NOW(6)+INTERVAL 1 MINUTE),"
                       "(102,'processing',NOW(6)+INTERVAL 1 MINUTE),"
                       "(103,'processing',NOW(6)-INTERVAL 1 SECOND)")
        cursor.execute("INSERT INTO llm_chatter_messages VALUES (100,0,'party')")
        migration = Path(__file__).resolve().parents[2] / (
            "data/sql/characters/updates/20261108_reactive_chat_scope.sql"
        )
        for _ in range(2):
            for statement in migration.read_text().split(";"):
                if statement.strip():
                    cursor.execute(statement)
        cursor.execute("SELECT COUNT(*) FROM llm_chatter_messages WHERE delivered=0")
        assert cursor.fetchone()[0] == 1
        db.commit()
    return lambda: connect(schema)


def test_concurrent_party_turns_and_retry_are_reserved_once(mysql_fixture):
    audience = "party:1:7:3:0:0:11,21,"
    entered, release = Event(), Event()

    def first_worker():
        with mysql_fixture() as db, reserve_party_reply(db, 101, "1:1", audience, 21, True, {}) as admitted:
            assert admitted
            entered.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_worker)
        try:
            assert entered.wait(10)
            with mysql_fixture() as db, reserve_party_reply(db, 102, "1:2", audience, 21, True, {}) as admitted:
                assert not admitted
        finally:
            release.set()
        first.result()

    for event_id, turn in [(101, "1:1"), (102, "1:1"), (103, "1:3")]:
        with mysql_fixture() as db, reserve_party_reply(db, event_id, turn, audience, 21, True, {}) as admitted:
            assert not admitted
    with mysql_fixture() as db, db.cursor() as cursor:
        cursor.execute("INSERT INTO llm_chatter_messages VALUES (101,0,'party')")
        db.commit()
        with reserve_party_reply(db, 102, "1:2", audience, 21, True, {}) as admitted:
            assert not admitted


def test_party_transcript_uses_full_audience_not_numeric_group_id(mysql_fixture):
    old = "party:1:7:3:0:0:11,21,"
    new = "party:1:7:4:0:0:11,21,"
    with mysql_fixture() as db, db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO llm_reactive_chat_history (audience,speaker_guid,speaker_name,is_bot,message) "
            "VALUES (%s,21,'Flint',1,'visible')", (old.encode(),),
        )
        assert [line["message"] for line in recent_transcript(db, old)] == ["visible"]
        assert recent_transcript(db, new) == []
        assert recent_transcript(db, "party:1:8:3:0:0:11,21,") == []
