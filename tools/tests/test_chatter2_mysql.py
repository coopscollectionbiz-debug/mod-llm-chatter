"""Whisper turn reservation against a disposable MySQL (CHATTER_TEST_MYSQL_PORT=3337).

Uses its own throwaway schema; skipped unless the disposable instance is
explicitly provided. Never run against a realm database.
"""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_normal_mode_player_chat  # noqa: F401,E402
import chatter_whisper  # noqa: E402

SCHEMA = 'chatter2_whisper_fixture'
AUDIENCE = 'whisper:1:11:21'


@pytest.fixture
def connect():
    if os.environ.get('CHATTER_TEST_MYSQL_PORT') != '3337':
        pytest.skip('disposable MySQL on loopback port 3337 required')
    import mysql.connector

    def open_db(database=SCHEMA):
        return mysql.connector.connect(host='127.0.0.1', port=3337, user='root', database=database)

    with open_db(None) as db, db.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE IF NOT EXISTS {SCHEMA}')
    with open_db() as db, db.cursor() as cursor:
        cursor.execute('DROP TABLE IF EXISTS llm_chatter_events, llm_chatter_messages, llm_reactive_reply_turn')
        cursor.execute('CREATE TABLE llm_chatter_events (id INT PRIMARY KEY, status VARCHAR(12), '
                       'expires_at DATETIME(6))')
        cursor.execute('CREATE TABLE llm_chatter_messages (event_id INT, delivered TINYINT, channel VARCHAR(12))')
        cursor.execute('CREATE TABLE llm_reactive_reply_turn (turn_key VARBINARY(96) PRIMARY KEY, '
                       'event_id INT UNSIGNED NOT NULL UNIQUE, audience VARBINARY(512) NOT NULL, '
                       'bot_guid INT UNSIGNED NOT NULL, admitted_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6))')
        cursor.execute("INSERT INTO llm_chatter_events VALUES (301,'processing',NOW(6)+INTERVAL 1 MINUTE),"
                       "(302,'processing',NOW(6)+INTERVAL 1 MINUTE),(303,'processing',NOW(6)-INTERVAL 1 SECOND)")
        db.commit()
    yield open_db
    with open_db(None) as db, db.cursor() as cursor:
        cursor.execute(f'DROP DATABASE IF EXISTS {SCHEMA}')


def reserve(open_db, event_id, turn):
    with open_db() as db:
        with chatter_whisper.reserve_whisper_reply(db, event_id, turn, AUDIENCE, 21, {}) as admitted:
            return admitted


def test_same_whisper_turn_is_admitted_once_under_concurrency(connect):
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: reserve(connect, 301, 'turn-a'), range(8)))
    assert results.count(True) == 1


def test_pending_reply_blocks_the_next_turn_and_expired_turns_never_admit(connect):
    assert reserve(connect, 301, 'turn-a')
    with connect() as db, db.cursor() as cursor:
        cursor.execute("INSERT INTO llm_chatter_messages VALUES (301, 0, 'whisper')")
        db.commit()
    assert not reserve(connect, 302, 'turn-b')   # pair still has an undelivered reply
    with connect() as db, db.cursor() as cursor:
        cursor.execute('UPDATE llm_chatter_messages SET delivered = 1')
        db.commit()
    assert reserve(connect, 302, 'turn-b')
    assert not reserve(connect, 303, 'turn-c')   # expired event
