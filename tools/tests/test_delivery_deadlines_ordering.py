"""Execute the production C++ deadline policy and fetch SQL with offline clocks.

The driver prints BuildFetchSql() from the same header used by worldserver.
SQLite supplies MySQL date functions for fixtures; MySQL EXPLAIN is a separate
read-only validation. No server, bridge, installed database or model is used.
"""
from datetime import datetime
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_delivery_performance import _build_policy_test, SRC
from test_relationship_memory import storage, DeliveryDB, outputs
import chatter_relationships as rel


@pytest.fixture(scope='module')
def policy_binary(tmp_path_factory):
    exe, result = _build_policy_test(tmp_path_factory.mktemp('deadline-policy'))
    if result is None:
        pytest.skip('no C++ compiler available')
    assert result.returncode == 0, result.stdout + result.stderr
    return exe


def run_policy(exe, *args):
    result = subprocess.run([str(exe), *map(str, args)], text=True, capture_output=True, check=True)
    return result.stdout.strip()


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def advance(self, seconds):
        self.value += seconds


class FetchFixture:
    def __init__(self, sql):
        self.clock = Clock()
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.create_function('NOW', -1, lambda *args: self.clock.value)
        self.db.create_function('DATE_ADD', 2, lambda value, seconds: value + seconds)
        self.db.create_function('DATE_SUB', 2, lambda value, seconds: value - seconds)
        self.db.create_function('TIMESTAMPDIFF', 3,
                                lambda unit, start, end: None if start is None or end is None
                                else round((end - start) * 1000000))
        self.db.create_function('IF', 3, lambda cond, yes, no: yes if cond else no)
        self.db.create_function('JSON_UNQUOTE', 1, lambda value: value)
        sql = sql.replace('INTERVAL 1 MICROSECOND', '0.000001')
        self.sql = re.sub(r'INTERVAL (\d+) SECOND', r'\1', sql)
        self.sql = self.sql.replace('TIMESTAMPDIFF(MICROSECOND,', "TIMESTAMPDIFF('MICROSECOND',")
        self.db.executescript('''
            CREATE TABLE llm_chatter_events (
                id INTEGER PRIMARY KEY, zone_id INTEGER, map_id INTEGER, extra_data TEXT,
                event_type TEXT, subject_guid INTEGER, target_entry INTEGER, target_guid INTEGER,
                priority INTEGER, expires_at REAL);
            CREATE TABLE llm_chatter_messages (
                id INTEGER PRIMARY KEY, bot_guid INTEGER, bot_name TEXT, message TEXT, channel TEXT,
                emote TEXT, npc_spawn_id INTEGER, player_guid INTEGER, sequence INTEGER,
                event_id INTEGER, group_id INTEGER, delivery_policy TEXT, delivery_reason TEXT,
                owner_subsystem TEXT, addressee_player_guid INTEGER, addressee_bot_guid INTEGER,
                addressee_npc_spawn_id INTEGER, queue_id INTEGER, delivered INTEGER,
                delivered_at REAL, deliver_at REAL, drop_reason TEXT);
            CREATE INDEX idx_event ON llm_chatter_messages(event_id);
            CREATE INDEX idx_queue ON llm_chatter_messages(queue_id);
            CREATE INDEX idx_delivery ON llm_chatter_messages(delivered, deliver_at);
        ''')

    def add(self, id, sequence=0, event_id=None, queue_id=None, channel='party', due=95, priority=10):
        if event_id is not None:
            self.db.execute('INSERT OR IGNORE INTO llm_chatter_events(id, priority) VALUES (?, ?)',
                            (event_id, priority))
        self.db.execute('INSERT INTO llm_chatter_messages '
                        '(id, bot_guid, bot_name, message, channel, sequence, event_id, queue_id, '
                        'delivered, deliver_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)',
                        (id, id + 100, f'Bot{id}', f'line{id}', channel, sequence, event_id, queue_id, due))

    def fetch(self):
        return [dict(row) for row in self.db.execute(self.sql)]

    def ids(self):
        return [r['id'] for r in self.fetch()]

    def retry(self, id):
        self.db.execute('UPDATE llm_chatter_messages SET deliver_at=? WHERE id=?',
                        (self.clock.value + 5, id))

    def terminal(self, id, reason=None):
        self.db.execute('UPDATE llm_chatter_messages SET delivered=1, delivered_at=?, '
                        'drop_reason=? WHERE id=?', (self.clock.value, reason, id))


@pytest.fixture
def fetched(policy_binary):
    fixture = FetchFixture(run_policy(policy_binary, '--fetch-sql'))
    yield fixture
    fixture.db.close()


@pytest.mark.parametrize('delay_us,expected', [
    (999999, 'send'), (1000000, 'send'), (1000001, 'expire'), (61000000, 'expire'),
])
def test_callback_wait_charged_against_remaining_db_lifetime(policy_binary, delay_us, expected):
    issued_at = 5000000000
    clock_us = issued_at + delay_us
    assert run_policy(policy_binary, '--deadline', 1000000, clock_us - issued_at, 1) == expected


def test_exact_expiry_boundary_uses_database_lifetime(fetched, policy_binary):
    fetched.add(1, due=40)  # DB now=100; exactly 60 s late.
    row = fetched.fetch()[0]
    assert row['remaining_us'] == 0
    assert run_policy(policy_binary, '--deadline', row['remaining_us'], 0, 1) == 'send'
    assert run_policy(policy_binary, '--deadline', row['remaining_us'], 1, 1) == 'expire'
    fetched.clock.advance(0.000001)
    assert fetched.ids() == []


def test_expiration_during_database_backlog_is_conservative(fetched, policy_binary):
    # The statement starts only after 61 seconds queued for a DB worker.
    issued_at = fetched.clock.value
    fetched.clock.advance(61)
    fetched.add(1, due=fetched.clock.value)
    row = fetched.fetch()[0]
    assert row['remaining_us'] == 60000000
    elapsed = round((fetched.clock.value - issued_at) * 1000000)
    assert run_policy(policy_binary, '--deadline', row['remaining_us'], elapsed, 1) == 'expire'


@pytest.mark.parametrize('server_wall_offset', [-86400, 0, 86400])
def test_clock_skew_never_enters_expiry_calculation(fetched, policy_binary, server_wall_offset):
    fetched.add(1, due=41)
    row = fetched.fetch()[0]
    local_wall = fetched.clock.value + server_wall_offset
    monotonic_clock = 9000000
    monotonic_clock += 1000001
    assert local_wall - fetched.clock.value == server_wall_offset
    assert run_policy(policy_binary, '--deadline', row['remaining_us'], monotonic_clock - 9000000, 1) == 'expire'


@pytest.mark.parametrize('delay', [0, 61000000])
def test_shutdown_while_query_pending_is_not_a_delivery(policy_binary, delay):
    assert run_policy(policy_binary, '--deadline', 1000000, delay, 0) == 'stop'


@pytest.mark.parametrize('identity', ['event', 'queue'])
@pytest.mark.parametrize('channel', ['party', 'general', 'guild', 'raid', 'battleground', 'say', 'msay'])
def test_retry_predecessor_blocks_across_ticks(fetched, identity, channel):
    key = {identity + '_id': 7}
    fetched.add(1, sequence=0, channel=channel, due=90, **key)
    fetched.add(2, sequence=1, channel=channel, due=95, **key)
    assert fetched.ids() == [1]
    for _ in range(3):
        fetched.retry(1)
        assert fetched.ids() == []
        fetched.clock.advance(5)
        assert fetched.ids() == [1]
    fetched.terminal(1)
    if channel in ('say', 'msay'):
        # Repeated retries may move scheduled predecessor past its successor;
        # a non-positive scheduled gap imposes no extra delay, just ordering.
        assert fetched.ids() == [2]
    else:
        assert fetched.ids() == [2]


@pytest.mark.parametrize('identity', ['event', 'queue'])
@pytest.mark.parametrize('reason', ['speaker_unavailable', 'expired_before_delivery'])
def test_terminal_predecessor_releases_ordinary_conversation(fetched, identity, reason):
    key = {identity + '_id': 3}
    fetched.add(1, sequence=0, **key)
    fetched.add(2, sequence=1, **key)
    fetched.terminal(1, reason)
    assert fetched.ids() == [2]


@pytest.mark.parametrize('channel', ['say', 'msay'])
@pytest.mark.parametrize('identity', ['event', 'queue'])
def test_local_spacing_waits_for_actual_delivery(fetched, channel, identity):
    key = {identity + '_id': 4}
    fetched.add(1, sequence=0, due=90, channel=channel, **key)
    fetched.add(2, sequence=1, due=95, channel=channel, **key)
    fetched.terminal(1)
    assert fetched.ids() == []
    fetched.clock.advance(4.999999)
    assert fetched.ids() == []
    fetched.clock.advance(0.000001)
    assert fetched.ids() == [2]


def test_all_lower_sequences_block_not_only_immediate_predecessor(fetched):
    fetched.add(1, event_id=3, sequence=0, due=105)
    fetched.add(3, event_id=3, sequence=2)
    assert fetched.ids() == []
    fetched.terminal(1, 'expired_before_delivery')
    assert fetched.ids() == [3]  # Missing sequence 1 never blocks forever.


def test_expired_pending_predecessor_waits_for_sweep_then_releases(fetched):
    fetched.add(1, event_id=3, sequence=0, due=30)
    fetched.add(2, event_id=3, sequence=1, due=95)
    assert fetched.ids() == []
    fetched.terminal(1, 'expired_before_delivery')
    assert fetched.ids() == [2]


def test_independent_conversations_progress_in_priority_order(fetched):
    fetched.add(1, event_id=3, sequence=0, due=105, priority=30)
    fetched.add(2, event_id=3, sequence=1, priority=30)
    fetched.add(3, event_id=4, priority=20)
    fetched.add(4, event_id=5, priority=10)
    fetched.add(5, queue_id=4)
    assert fetched.ids() == [3, 4, 5]


def test_event_identity_wins_over_queue_identity(fetched):
    fetched.add(1, queue_id=7, due=105)
    fetched.add(2, event_id=4, queue_id=7, sequence=1)
    assert fetched.ids() == [2]


def test_indexed_conversation_probes(fetched):
    fetched.add(1, event_id=3)
    plan = '\n'.join(str(tuple(r)) for r in fetched.db.execute('EXPLAIN QUERY PLAN ' + fetched.sql))
    assert 'idx_event' in plan and 'idx_queue' in plan


def test_expired_output_cannot_confirm_relationships_or_memories(storage):
    store, _ = storage
    store.pending('event:20', outputs(), 'party', {1, 2})
    rows = [{'id': g + 10, 'bot_guid': g, 'message': f'line{g}', 'delivered': 1,
             'delivered_at': datetime.now(), 'drop_reason': 'expired_before_delivery'} for g in (1, 2)]
    rel._poll(DeliveryDB(rows))
    assert store.relationship(1, 2) == rel.neutral()
    assert store.relationship(2, 1) == rel.neutral()
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM relationship_memories').fetchone()[0] == 0


def test_deadline_check_precedes_all_primary_sends_and_terminal_expiry_is_not_success():
    source = (SRC / 'LLMChatterDelivery.cpp').read_text(encoding='utf8')
    row = source[source.index('static DeliveryOutcome DeliverRow('):source.index('constexpr uint32 DELIVERY_EXPIRY_SECONDS')]
    # Main bot branches are before optional post-speech animation.
    primary = row[:row.index('            if (sent\n                && !emoteOnly')]
    send_names = ('PlayUnitTextEmoteAnimation', 'SendBotTextEmote', 'SendPartyMessageInstant',
                  'ai->SayToParty', 'grp->BroadcastPacket', 'ai->Say(', 'guild->BroadcastToGuild',
                  'ai->Yell', 'ch->Say', 'ai->SayToChannel')
    for number, line in enumerate(primary.splitlines()):
        if any(name in line for name in send_names):
            assert 'return DeliveryOutcome::Expired;' in primary.splitlines()[number - 1]
            assert 'if (!withinDeadline())' in primary.splitlines()[number - 2]
    for name in ('speaker->Say(', 'SendUnitTextEmote(', 'speaker->Yell('):
        at = row.index(name)
        assert 'return DeliveryOutcome::Expired;' in row[max(0, at - 130):at]
    process = source[source.index('void ProcessFetchedRows('):source.index('void SweepExpiredMessages(')]
    expired = process[process.index('case DeliveryOutcome::Expired:'):process.index('case DeliveryOutcome::Sent:')]
    assert "drop_reason = 'expired_before_delivery'" in expired
    assert '_handledRows.Mark' in expired and 'sentIds.push_back' not in expired
    assert 'CheckDispatch(' in process and 'steady_clock::now() - issuedAt' in process
