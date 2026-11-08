"""Chatter 2.0 privacy audit: every memory/relationship reader and writer.

Readers
- ``chatter_memory.get_bot_memories`` / ``get_relationship_memory_context``
  (legacy llm_bot_memories rows; six call sites in chatter_group, one in
  chatter_relationships): recall only when the current scope is non-public
  and its audience is exactly the original bot/player pair.
- ``RelationshipStore.recall`` via ``enrich_prompt``: audience-scoped rows.
- Surface engine DATA: server data and live observations only, filtered by
  the surface audience; never memories (source 'memory' never rendered).

Writers
- ``queue_relationship_memory`` / ``observe_input``: only the real input of
  the event being dispatched, by its actual sender, to a bot that replied.
- Generated bot lines: familiarity only after confirmed delivery.
"""

from pathlib import Path
import re
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import chatter_memory  # noqa: E402
import chatter_relationships as rel  # noqa: E402
import chatter_surfaces as surfaces  # noqa: E402
from chatter_evidence import Evidence, render_block  # noqa: E402


class Cursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append(sql)

    def fetchall(self):
        return [{'id': 1, 'memory': 'Helped me through Deadmines'}]

    def close(self):
        pass


class DB:
    def __init__(self):
        self.cur = Cursor()

    def cursor(self, **_):
        return self.cur


@pytest.mark.parametrize('channel,audience,allowed', [
    ('general', {7, 99}, False),      # public: never
    ('say', {7, 99}, False),
    ('guild', {7, 99, 8}, False),     # guildmates beyond the pair
    ('party', {7, 99, 8}, False),     # a third member would hear it
    ('party', {7, 99}, True),         # just the two of them
    ('whisper', {7, 99}, True),
    ('party', set(), False),          # missing membership provenance fails closed
])
def test_legacy_memory_recall_requires_private_dyad(channel, audience, allowed):
    db = DB()
    token = rel._scope.set(rel.Conversation('k', channel, audience=audience))
    try:
        rows = chatter_memory.get_bot_memories(db, 7, 99)
        context = chatter_memory.get_relationship_memory_context(db, 7, 99, 'Coop')
    finally:
        rel._scope.reset(token)
    assert bool(rows) is allowed
    assert bool(context) is allowed
    if not allowed:
        assert not db.cur.executed, 'a denied read must not even query'


def test_no_scope_means_no_recall():
    assert chatter_memory.get_bot_memories(DB(), 7, 99) == []


def test_every_memory_read_goes_through_the_gate():
    source = (TOOLS / 'chatter_memory.py').read_text(encoding='utf-8')
    reader = source[source.index('def get_bot_memories'):source.index('def sanitize_memory_for_prompt')]
    assert reader.index('legacy_allowed(bot_guid, player_guid)') < reader.index('cursor.execute')
    direct = []
    for path in TOOLS.glob('*.py'):
        # llm_chatter_bridge._write_db_snapshot is the operator log-viewer
        # snapshot (local file, never a prompt); audited separately.
        if path.name in ('chatter_memory.py', 'llm_chatter_bridge.py'):
            continue
        text = path.read_text(encoding='utf-8')
        if re.search(r'FROM\s+llm_bot_memories', text, re.I) and not re.search(
                r'(UPDATE|DELETE|INSERT)[^;]{0,80}llm_bot_memories', text, re.I):
            direct.append(path.name)
    assert not direct, f'unexpected direct memory readers: {direct}'


def test_operator_snapshot_is_not_a_prompt_reader():
    bridge = (TOOLS / 'llm_chatter_bridge.py').read_text(encoding='utf-8')
    start = bridge.index('def _write_db_snapshot')
    body = bridge[start:bridge.index('\ndef ', start + 10)]
    assert 'FROM llm_bot_memories' in body and 'call_llm' not in body
    assert bridge.count('FROM llm_bot_memories') == 1


def test_writer_accepts_only_the_dispatched_input():
    current = rel.Conversation('event:1', 'party', actor=99, actor_name='Coop', text='I prefer to tank',
                               audience={7, 99}, outputs=[{'id': 1, 'guid': 7}])
    token = rel._scope.set(current)
    saved = rel._store
    rel._store = object()  # writer must reject before touching the store
    try:
        assert rel.queue_relationship_memory({}, 7, 99, 'I prefer healing', 'party_player') is False  # not the input
        assert rel.queue_relationship_memory({}, 7, 42, 'I prefer to tank', 'party_player') is False  # not the sender
        assert rel.queue_relationship_memory({}, 8, 99, 'I prefer to tank', 'party_player') is False  # did not reply
        assert rel.queue_relationship_memory({}, 7, 99, 'I prefer to tank', 'general_player') is False  # wrong channel
        batch = []
        assert rel.queue_relationship_memory({}, 7, 99, 'I prefer to tank', 'party_player', batch=batch) is True
        assert batch and batch[0].audience == (7, 99)
    finally:
        rel._store = saved
        rel._scope.reset(token)


def test_public_render_drops_party_and_private_evidence():
    evidence = [
        Evidence('quest', 'server_data', 'found', {'title': 'public quest'}),
        Evidence('live_self', 'observed', 'found', {'mana_pct': 12}, audience='party'),
        Evidence('relationship', 'memory', 'found', {'note': 'secret'}, audience='private'),
    ]
    block, used = render_block(evidence, 'public')
    assert 'public quest' in block and 'mana_pct' not in block and 'secret' not in block


def test_surface_engine_never_reads_memories():
    text = (TOOLS / 'chatter_surfaces.py').read_text(encoding='utf-8')
    assert 'llm_bot_memories' not in text and 'get_bot_memories' not in text
    for policy in surfaces.SURFACES.values():
        if policy.channel in ('general', 'say', 'guild'):
            assert policy.audience == 'public'
