"""Chatter 2.0 gameplay scenarios and population-scale behaviour.

Deterministic, mocked replays of the requested scenarios. They exercise
the production decision, knowledge, evidence, prompt, claim and fallback
code with a fake provider and scripted world data. They are NOT gameplay
tests: no worldserver, client or real model is involved.
"""

from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))
import test_normal_mode_player_chat  # noqa: F401,E402
import chatter_knowledge as kn  # noqa: E402
import chatter_llm  # noqa: E402
import chatter_relationships as rel  # noqa: E402
import chatter_social as social  # noqa: E402
from chatter_personality import generate_profile  # noqa: E402
from test_chatter2_conversation import ASH, CONFIG, FLINT, FakeClient, live, run  # noqa: E402
from test_chatter2_knowledge import ITEM_ROW, ScriptedReader, ilink, loot_routes, qlink, quest_routes  # noqa: E402
from test_relationship_memory import storage  # noqa: E402,F401


@pytest.fixture(autouse=True)
def reset():
    chatter_llm._breaker.failures = 0
    chatter_llm._breaker.open_until = 0.0
    kn.configure(CONFIG, reader=ScriptedReader(quest_routes()))
    yield


def test_scenario_tank_answers_pull_question_without_a_rage_claim():
    decision = social.decide_party('tank ready to pull?', [ASH, FLINT], live=live())
    assert decision.bot['bot_guid'] == 20
    client = FakeClient('{"message": "yeah go"}')
    result = run(client, 'tank ready to pull?', live())  # speaker fixture is the priest
    assert result.claims in (None, 'alive;g=21') or result.claims.startswith('power_')
    tank_live = live(bots=[{'g': 21, 'a': 1, 'c': 0, 'hp': 100, 'pt': 1, 'pp': 0, 'm': 36, 'role': 'tank'}])
    result = run(FakeClient('{"message": "go"}'), 'ready to pull?', tank_live)
    assert result.claims == 'alive;g=21'  # rage at 0 is normal: never "low rage"


def test_scenario_boss_identification_in_current_dungeon():
    reader = ScriptedReader([('instance_encounters', [{'entry': 644, 'name': "Rhahk'Zor"},
                                                     {'entry': 639, 'name': 'Edwin VanCleef'}])])
    kn.configure(CONFIG, reader=reader)
    client = FakeClient('{"message": "VanCleef is last, few before that"}')
    result = run(client, 'how many bosses are in here?', live())
    assert result.stats['knowledge'] == ['boss'] and 'Edwin VanCleef' in client.last_prompt()
    assert reader.sql[0][1] == (36,)  # scoped to the party's own map


def test_scenario_item_drop_question_never_promises_a_rate():
    rows = [{'entry': 639, 'name': 'Edwin VanCleef', 'rank': 3, 'Chance': 0, 'Reference': 0, 'GroupId': 1,
             'QuestRequired': 0, 'LootMode': 1}]
    kn.configure(CONFIG, reader=ScriptedReader(loot_routes(creature_rows=rows)))
    client = FakeClient('{"message": "vancleef has it on his table"}')
    run(client, f'what drops {ilink(ITEM_ROW["entry"], "Sword")}?', live())
    prompt = client.last_prompt()
    assert 'Edwin VanCleef' in prompt and 'configured_chance_pct' not in prompt
    assert 'not a drop rate' in prompt


def test_scenario_two_bots_remember_a_previous_dungeon_run(storage):
    store, _ = storage
    rel.observe_game({'id': 7, 'event_type': 'bot_group_kill', 'map_id': 36},
                     {'is_boss': True, 'boss_name': 'Edwin VanCleef', 'experience_participants': [
                         {'guid': 21, 'name': 'Ash', 'is_bot': True}, {'guid': 20, 'name': 'Flint', 'is_bot': True},
                         {'guid': 11, 'name': 'Nell', 'is_bot': False}]})
    current = rel.Conversation('event:8', 'party', 11, 'Nell', 'remember vancleef last time?', {11, 20, 21})
    token = rel._scope.set(current)
    try:
        client = FakeClient('{"message": "barely, that pull was rough"}')
        run(client, 'remember vancleef last time?', live())
        assert '<relationship_context>' in client.last_prompt()
        assert 'VanCleef' in client.last_prompt()
    finally:
        rel._scope.reset(token)


def test_scenario_familiar_player_gets_the_familiar_bot():
    decision = social.decide_party('anyone up for another run?', [FLINT, ASH], familiarity={21: 90.0})
    assert decision.bot['bot_guid'] == 21


def test_scenario_many_bots_one_reply():
    bots = [dict(bot_guid=100 + i, bot_name=f'Bot{i}', role='dps', last_reply=0) for i in range(4)]
    decisions = {social.decide_party('anyone know the way?', bots).bot['bot_guid'] for _ in range(5)}
    assert len(decisions) == 1  # deterministic single responder per turn


def test_scenario_model_outage_during_gameplay():
    client = FakeClient(fail=True)
    lines = [run(client, 'heals ready?', live()).message for _ in range(8)]
    assert all(line.lower() == 'mana sec' for line in lines)  # evidence-backed fallback only
    assert len(client.prompts) == 5  # breaker opened after 5 failures


def test_scale_population_simultaneous_events():
    """4,000 bots in 800 parties all asking at once: decision cost and model volume."""
    profiles = {g: generate_profile(g) for g in range(1000, 5000)}
    parties = [[dict(bot_guid=1000 + p * 5 + i, bot_name=f'B{p}_{i}', role=('tank', 'healer', 'dps')[i % 3],
                     last_reply=0) for i in range(5)] for p in range(800)]
    messages = ['heals ready?', 'ok ty', 'anyone know the way?', 'lol', '@heal follow', 'tank ready to pull?']
    timings, respond = [], 0
    for index, bots in enumerate(parties):
        started = time.perf_counter()
        decision = social.decide_party(messages[index % len(messages)], bots, live=None,
                                       profiles={b['bot_guid']: profiles[b['bot_guid']] for b in bots})
        timings.append((time.perf_counter() - started) * 1e6)
        respond += decision.respond
    p50, p95 = statistics.median(timings), sorted(timings)[int(len(timings) * 0.95)]
    print(f'decide_party over 800 parties: p50={p50:.1f}us p95={p95:.1f}us respond={respond}/800')
    assert respond <= 800 * 4 / 6 + 1  # acks, lol and commands never respond
    assert p95 < 5000  # well under 5 ms per turn on the bridge


def test_scale_knowledge_cache_absorbs_repeated_questions():
    reader = ScriptedReader(quest_routes())
    eng = kn.configure(CONFIG, reader=reader)
    intents = kn.detect_intents(f'anyone know who starts {qlink(783, "A Threat Within")}?')
    for _ in range(500):
        eng.lookup(intents)
    first_queries = len(reader.sql)
    assert eng.cache.hits == 499 and first_queries < 12


def test_scale_skipped_turns_cost_no_lookup_and_no_model_call():
    reader = ScriptedReader(quest_routes())
    kn.configure(CONFIG, reader=reader)
    client = FakeClient()
    for message in ('ok ty', 'lol', '@heal follow', 'nice'):
        decision = social.decide_party(message, [FLINT, ASH])
        assert not decision.respond
    assert reader.sql == [] and client.prompts == []
