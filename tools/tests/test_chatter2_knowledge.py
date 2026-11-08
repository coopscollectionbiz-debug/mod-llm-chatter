"""Chatter 2.0 knowledge engine: intents, evidence boundaries, failures.

Unit tests use a scripted reader (no database). The integration class runs
read-only against the local development world database when reachable and
skips otherwise; it never writes and never touches production.
"""

from pathlib import Path
import re
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_normal_mode_player_chat  # noqa: F401,E402 - optional SDK stubs
import chatter_knowledge as kn  # noqa: E402


def qlink(qid, label, level=10):
    return f'|cffffff00|Hquest:{qid}:{level}|h[{label}]|h|r'


def ilink(item, label):
    return f'|cff0070dd|Hitem:{item}:0:0:0:0:0:0:0|h[{label}]|h|r'


class ScriptedReader:
    """Matches SQL fragments to canned rows; records every query."""

    def __init__(self, routes, delay=0.0, fail=False):
        self.routes = routes
        self.delay = delay
        self.fail = fail
        self.sql = []

    def query(self, sql, params=(), deadline=None):
        if deadline is not None and time.monotonic() >= deadline:
            raise kn.KnowledgeTimeout('deadline')
        if self.fail:
            raise ConnectionError('db down')
        time.sleep(self.delay)
        self.sql.append((sql, params))
        for fragment, rows in self.routes:
            if fragment in sql:
                return rows(params) if callable(rows) else list(rows)
        return []

    def revision(self):
        return 'r1'

    def close(self):
        pass


QUEST_ROW = {
    'ID': 783, 'LogTitle': 'A Threat Within', 'QuestLevel': 1, 'MinLevel': 1, 'AllowableRaces': 0, 'Flags': 0,
    'ObjectiveText1': '', 'ObjectiveText2': '', 'ObjectiveText3': '', 'ObjectiveText4': '',
    'RequiredNpcOrGo1': 0, 'RequiredNpcOrGo2': 0, 'RequiredNpcOrGo3': 0, 'RequiredNpcOrGo4': 0,
    'RequiredNpcOrGoCount1': 0, 'RequiredNpcOrGoCount2': 0, 'RequiredNpcOrGoCount3': 0, 'RequiredNpcOrGoCount4': 0,
    'RequiredItemId1': 0, 'RequiredItemId2': 0, 'RequiredItemId3': 0, 'RequiredItemId4': 0,
    'RequiredItemCount1': 0, 'RequiredItemCount2': 0, 'RequiredItemCount3': 0, 'RequiredItemCount4': 0,
    'PrevQuestID': 0, 'NextQuestID': 7, 'ExclusiveGroup': 0, 'AllowableClasses': 0, 'MaxLevel': 0,
    'BreadcrumbForQuestId': 0,
}


def quest_routes(starters=(('npc', 823, 'Deputy Willem'),), quest=QUEST_ROW, conditions=0, same_title=0):
    return [
        ('FROM quest_template q LEFT JOIN', [quest] if quest else []),
        ('creature_queststarter', [{'type': t, 'entry': e, 'name': n} for t, e, n in starters]),
        ("'item' AS type", []),
        ('FROM creature WHERE id', [{'map': 0, 'zoneId': 12, 'x': -8900.0, 'y': -100.0}]),
        ('FROM gameobject WHERE id', [{'map': 0, 'zoneId': 0, 'x': -8900.0, 'y': -100.0}]),
        ('WHERE LogTitle = %s AND ID <> %s', [{'n': same_title}]),
        ('FROM conditions', [{'n': conditions}]),
        ('FROM quest_template WHERE ID IN', [{'id': 7, 'name': 'Kobold Camp Cleanup'}]),
    ]


def engine(reader, **config):
    return kn.KnowledgeEngine({'LLMChatter.Knowledge.Enable': '1', **config}, reader=reader)


# ---------------------------------------------------------------------------
# Intent recognition
# ---------------------------------------------------------------------------

def test_quest_link_routes_by_question_shape():
    assert [i.kind for i in kn.detect_intents(f'who starts {qlink(783, "A Threat Within")}?')] == ['quest_starter']
    assert [i.kind for i in kn.detect_intents(f'where do i turn in {qlink(783, "A Threat Within")}')] == [
        'quest_ender']
    assert [i.kind for i in kn.detect_intents(f'what lvl is {qlink(783, "x")}?')] == ['quest_info']
    assert 'quest_eligibility' in [i.kind for i in kn.detect_intents(f'can i take {qlink(783, "x")}?')]


def test_item_link_routes():
    assert [i.kind for i in kn.detect_intents(f'what drops {ilink(1, "Sword")}?')] == ['item_source']
    assert [i.kind for i in kn.detect_intents(f'does this boss drop {ilink(1, "Sword")}?')] == ['boss_drop']
    assert [i.kind for i in kn.detect_intents(f'is {ilink(1, "Sword")} good?')] == ['item_info']


def test_plain_text_titles_are_never_resolved():
    # Same-title quests exist; only native links carry an exact ID.
    assert kn.detect_intents('who starts A Threat Within?') == []
    assert kn.detect_intents('lol') == []


def test_forged_or_malformed_links_are_ignored():
    assert kn.parse_links('|Hquest:783:1|h[A Threat Within]|h') == []          # no colour/terminator
    assert kn.parse_links('|cffffff00|Hquest:abc|h[x]|h|r') == []              # non-numeric id
    assert kn.parse_links('|cffffff00|Hquest:783|h[x|y]|h|r') == []            # pipe in label
    assert len(kn.parse_links(''.join(qlink(i, 'q') for i in range(1, 6)))) == 3  # bounded


def test_service_questions_route_without_links():
    intent = kn.detect_intents('anyone know where the nearest warrior trainer is?')[0]
    assert intent.kind == 'npc_service' and intent.extra == {'flag': 'trainer', 'specialty': 'warrior'}
    assert kn.detect_intents('where is the fp')[0].extra['flag'] == 'flightmaster'
    assert kn.detect_intents('how many bosses are in here?')[0].kind == 'boss_list'


# ---------------------------------------------------------------------------
# Evidence boundaries
# ---------------------------------------------------------------------------

def test_quest_starter_is_configured_fact_not_eligibility():
    reader = ScriptedReader(quest_routes())
    found, stats = engine(reader).lookup(kn.detect_intents(f'who starts {qlink(783, "A Threat Within")}?'))
    evidence = found[0]
    assert evidence.source == 'server_data' and evidence.status == 'found' and evidence.audience == 'public'
    assert evidence.facts['started_by'][0]['name'] == 'Deputy Willem'
    assert evidence.facts['started_by'][0]['spawn_zone'] == 'Elwynn Forest'
    assert 'not checked' in evidence.facts['eligibility']
    assert stats['lookups'] == 1 and stats['failed'] == 0
    assert all('%s' in sql or 'updates' in sql for sql, _ in reader.sql)  # parameterised


def test_forged_link_label_uses_real_title():
    reader = ScriptedReader(quest_routes())
    found, _ = engine(reader).lookup(kn.detect_intents(f'who starts {qlink(783, "Free Epics Here")}?'))
    assert found[0].facts['title'] == 'A Threat Within'
    assert found[0].facts['link_label_mismatch'] is True
    assert 'real title' in found[0].note


def test_custom_quest_ids_resolve_like_any_other():
    custom = dict(QUEST_ROW, ID=90001, LogTitle='Worgen Trial')
    reader = ScriptedReader(quest_routes(starters=(('npc', 91000, 'Custom Warden'),), quest=custom))
    found, _ = engine(reader).lookup(kn.detect_intents(f'who starts {qlink(90001, "Worgen Trial")}?'))
    assert found[0].facts['quest_id'] == 90001 and found[0].facts['started_by'][0]['name'] == 'Custom Warden'


def test_multiple_starters_are_ambiguous_and_same_titles_flagged():
    reader = ScriptedReader(quest_routes(starters=(('npc', 1, 'Mira'), ('object', 2, 'Wanted Poster')),
                                         same_title=2))
    evidence = engine(reader).lookup(kn.detect_intents(f'who starts {qlink(783, "A Threat Within")}?'))[0][0]
    assert evidence.status == 'ambiguous'
    assert evidence.facts['same_title_other_ids'] == 2
    assert {s['type'] for s in evidence.facts['started_by']} == {'npc', 'object'}


def test_unknown_quest_id_is_not_found_not_invented():
    reader = ScriptedReader(quest_routes(quest=None))
    evidence = engine(reader).lookup(kn.detect_intents(f'who starts {qlink(999999, "Nope")}?'))[0][0]
    assert evidence.status == 'not_found'


def test_quest_eligibility_is_unsupported_from_server_data():
    reader = ScriptedReader(quest_routes())
    found, _ = engine(reader).lookup(kn.detect_intents(f'can i take {qlink(783, "A Threat Within")}?'))
    eligibility = [e for e in found if e.status == 'unsupported']
    assert eligibility and 'progression' in eligibility[0].note


def test_conditional_quests_report_conditions():
    reader = ScriptedReader(quest_routes(conditions=2))
    evidence = engine(reader).lookup(kn.detect_intents(f'what lvl is {qlink(783, "A Threat Within")}?'))[0][0]
    assert evidence.facts['eligibility'] == 'not checked (2 configured conditions)'
    assert evidence.facts['chain'] == {'leads_to': 'Kobold Camp Cleanup'}


ITEM_ROW = {'entry': 1, 'name': 'Sword', 'Quality': 3, 'class': 2, 'subclass': 7, 'InventoryType': 13,
            'ItemLevel': 20, 'RequiredLevel': 15, 'AllowableClass': -1, 'AllowableRace': -1, 'bonding': 1,
            'RequiredSkill': 0, 'RequiredSkillRank': 0, 'startquest': 0}


def loot_routes(creature_rows=(), refs=(), conditions=0, vendors=()):
    return [
        ('FROM item_template WHERE entry', [ITEM_ROW]),
        ('FROM reference_loot_template WHERE Item', [{'Entry': r} for r in refs]),
        ('FROM reference_loot_template WHERE Reference IN', []),
        ('FROM creature_loot_template l', list(creature_rows)),
        ('FROM gameobject_loot_template', []),
        ('FROM npc_vendor', [{'name': v} for v in vendors]),
        ('FROM conditions', [{'n': conditions}]),
    ]


def test_loot_reports_configured_chance_only_for_plain_rows():
    reader = ScriptedReader(loot_routes(creature_rows=[
        {'entry': 5, 'name': 'Rhahk Zor', 'rank': 3, 'Chance': 12.5, 'Reference': 0, 'GroupId': 0,
         'QuestRequired': 0, 'LootMode': 1},
        {'entry': 6, 'name': 'Sneed', 'rank': 3, 'Chance': 0, 'Reference': 0, 'GroupId': 1,
         'QuestRequired': 0, 'LootMode': 1},
    ]))
    evidence = engine(reader).lookup(kn.detect_intents(f'what drops {ilink(1, "Sword")}?'))[0][0]
    first, grouped = evidence.facts['creature_sources']
    assert first['configured_chance_pct'] == 12.5
    assert 'configured_chance_pct' not in grouped and 'not computed' in grouped['chance']
    assert 'scripted or encounter-chest' in evidence.note


def test_reference_loot_is_expanded_and_conditions_make_it_partial():
    reader = ScriptedReader(loot_routes(refs=[44007], conditions=1, creature_rows=[
        {'entry': 7, 'name': 'Defias Pirate', 'rank': 0, 'Chance': 100, 'Reference': 44007, 'GroupId': 0,
         'QuestRequired': 0, 'LootMode': 1}]))
    evidence = engine(reader).lookup(kn.detect_intents(f'where do i get {ilink(1, "Sword")}?'))[0][0]
    assert evidence.status == 'partial'
    assert evidence.facts['creature_sources'][0]['chance'].startswith('shared loot group')
    assert any('44007' in str(params) for sql, params in reader.sql if 'creature_loot_template' in sql)


def test_item_with_no_configured_source_is_not_absence():
    reader = ScriptedReader(loot_routes())
    evidence = engine(reader).lookup(kn.detect_intents(f'what drops {ilink(1, "Sword")}?'))[0][0]
    assert evidence.status == 'not_found'
    assert 'scripted' in evidence.note


def test_boss_drop_absent_from_boss_table_stays_partial():
    routes = loot_routes() + [('ct.difficulty_entry_1', [
        {'entry': 644, 'name': "Rhahk'Zor", 'difficulty_entry_1': 0, 'difficulty_entry_2': 0,
         'difficulty_entry_3': 0}])]
    reader = ScriptedReader(routes)
    intents = kn.detect_intents(f"does rhahk'zor drop {ilink(1, 'Sword')}?")
    intents[0].kind, intents[0].extra = 'boss_drop', {'boss_text': "does rhahk'zor drop [sword]?"}
    evidence = engine(reader).lookup(intents, world={'map_id': 36})[0][0]
    assert evidence.facts['boss'] == "Rhahk'Zor" and evidence.facts['on_boss_table'] is False
    assert evidence.status == 'partial'


def test_npc_service_needs_position_and_never_claims_access():
    reader = ScriptedReader([('npcflag & %s', [
        {'name': 'Llane Beshere', 'subname': 'Warrior Trainer', 'x': -8918.0, 'y': -208.0}])])
    intents = kn.detect_intents('where is the nearest warrior trainer')
    assert engine(reader).lookup(intents, world={})[0][0].status == 'unsupported'
    evidence = engine(reader).lookup(intents, world={'map_id': 0, 'x': -8900, 'y': -200})[0][0]
    assert evidence.facts['nearest'][0]['name'] == 'Llane Beshere'
    assert 'path not verified' in evidence.note


# ---------------------------------------------------------------------------
# Failure behaviour, caching, deadlines
# ---------------------------------------------------------------------------

def test_database_failure_becomes_unavailable_not_exception():
    found, stats = engine(ScriptedReader([], fail=True)).lookup(
        kn.detect_intents(f'who starts {qlink(783, "x")}?'))
    assert found[0].status == 'unavailable' and stats['failed'] == 1


def test_expired_deadline_does_no_query():
    reader = ScriptedReader(quest_routes())
    found, _ = engine(reader).lookup(kn.detect_intents(f'who starts {qlink(783, "x")}?'),
                                     deadline=time.monotonic() - 1)
    assert found[0].status == 'unavailable' and reader.sql == []


def test_results_are_cached_by_world_revision():
    reader = ScriptedReader(quest_routes())
    eng = engine(reader)
    intents = kn.detect_intents(f'who starts {qlink(783, "A Threat Within")}?')
    eng.lookup(intents)
    count = len(reader.sql)
    _, stats = eng.lookup(intents)
    assert stats['cache_hits'] == 1 and len(reader.sql) == count


def test_lookups_per_turn_are_bounded():
    reader = ScriptedReader(quest_routes())
    message = 'who starts ' + qlink(1, 'a') + ' and ' + qlink(2, 'b')
    _, stats = engine(reader, **{'LLMChatter.Knowledge.MaxLookupsPerTurn': 1}).lookup(kn.detect_intents(message))
    assert stats['lookups'] == 1


def test_disabled_engine_does_nothing():
    reader = ScriptedReader(quest_routes())
    found, _ = engine(reader, **{'LLMChatter.Knowledge.Enable': '0'}).lookup(
        kn.detect_intents(f'who starts {qlink(783, "x")}?'))
    assert found == [] and reader.sql == []


def test_world_reader_sets_read_only_and_statement_cap():
    executed = []

    class Cursor:
        def __init__(self, dictionary=False):
            pass

        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchall(self):
            return []

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=False):
            return Cursor()

        def close(self):
            pass

    reader = kn.WorldReader({'LLMChatter.Knowledge.QueryTimeoutMs': '250'}, connect=Conn)
    reader.query('SELECT 1')
    reader.query('SELECT 2')
    assert executed[:2] == ['SET SESSION TRANSACTION READ ONLY', 'SET SESSION MAX_EXECUTION_TIME = 250']
    assert executed.count('SET SESSION TRANSACTION READ ONLY') == 1  # pooled connection reused


# ---------------------------------------------------------------------------
# Read-only integration with the local development world database
# ---------------------------------------------------------------------------

def _local_config():
    path = Path(r'C:\Azerothcore-Install\configs\modules\mod_llm_chatter.conf')
    if not path.exists():
        return None
    text = path.read_text(encoding='utf-8', errors='replace')
    values = dict(re.findall(r'^(LLMChatter\.Database\.\w+)\s*=\s*"?([^"\r\n]*)"?', text, re.M))
    return {k: v.strip() for k, v in values.items()}


@pytest.fixture(scope='module')
def live_engine():
    config = _local_config()
    if not config:
        pytest.skip('no local development configuration')
    try:
        import mysql.connector  # noqa: F401
        eng = kn.KnowledgeEngine(dict(config, **{'LLMChatter.Knowledge.ConnectTimeoutSeconds': '1'}))
        eng.reader.query('SELECT 1')
    except Exception as exc:
        pytest.skip(f'local world database unavailable: {type(exc).__name__}')
    yield eng
    eng.reader.close()


class TestLocalWorldDatabase:
    def test_real_starter_lookup(self, live_engine):
        found, stats = live_engine.lookup(kn.detect_intents(f'who starts {qlink(783, "A Threat Within")}?'))
        assert found[0].status in ('found', 'ambiguous')
        assert any(s['name'] == 'Deputy Willem' for s in found[0].facts['started_by'])
        assert stats['ms'] < 500

    def test_real_custom_quest_ids(self, live_engine):
        rows = live_engine.reader.query('SELECT ID, LogTitle FROM quest_template WHERE ID >= 90000 LIMIT 1')
        if not rows:
            pytest.skip('no custom quests on this realm')
        quest = rows[0]
        found, _ = live_engine.lookup(kn.detect_intents(f'what lvl is {qlink(quest["ID"], quest["LogTitle"])}?'))
        assert found[0].facts['quest_id'] == quest['ID'] and found[0].status == 'found'

    def test_real_dungeon_bosses(self, live_engine):
        found, _ = live_engine.lookup(kn.detect_intents('how many bosses are in here?'), world={'map_id': 36})
        assert found[0].status == 'found' and "Rhahk'Zor" in found[0].facts['bosses']

    def test_real_item_sources_are_bounded(self, live_engine):
        started = time.monotonic()
        found, _ = live_engine.lookup(kn.detect_intents(f'what drops {ilink(2589, "Linen Cloth")}?'))
        assert time.monotonic() - started < 2.0
        assert found[0].status in ('found', 'partial')
        assert len(found[0].facts.get('creature_sources', ())) <= 8

    def test_real_service_lookup_near_goldshire(self, live_engine):
        found, _ = live_engine.lookup(kn.detect_intents('where is the nearest warrior trainer'),
                                      world={'map_id': 0, 'x': -9465, 'y': 64})
        assert found[0].status == 'found' and found[0].facts['nearest'][0]['yards_straight_line'] < 400
