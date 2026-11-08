"""Chatter 2.0 shared surface engine and conversation lifecycle.

Covers: every label and event type resolves to an explicit surface policy;
the rollback switch makes the engine inert; handled/system surfaces are
untouched; rules, audience-filtered knowledge and RSS reach other
surfaces; lifecycle precedence, pacing, reply-chain limits and repeat
suppression; and the source contracts at each integration point.
"""

from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import chatter_lifecycle  # noqa: E402
import chatter_surfaces as surfaces  # noqa: E402
from chatter_evidence import Evidence  # noqa: E402
from chatter_shared import PromptParts  # noqa: E402

ON = {'LLMChatter.Dialogue.Pipeline': '2', 'LLMChatter.Surfaces.Enable': '1', 'LLMChatter.ChatterMode': 'normal'}


@pytest.fixture(autouse=True)
def engine_on():
    surfaces.configure(ON)
    yield
    surfaces.configure({'LLMChatter.Surfaces.Enable': '0'})


def _labels():
    found = set()
    for path in TOOLS.glob('*.py'):
        found |= set(re.findall(r"label=['\"]([a-z_0-9]+)['\"]", path.read_text(encoding='utf-8')))
        found |= set(re.findall(r"\b_[a-z]+_label\s*=\s*\(?\s*['\"]([a-z_0-9]+)['\"]", path.read_text(encoding='utf-8')))
    return found


SYSTEM_LABELS = {'memory_generation', 'bot_tone', 'bot_backstory', 'guild_session_summary',
                 'find_addressed_bot', 'screenshot_vision'}


def test_every_label_has_an_explicit_surface():
    labels = _labels()
    assert len(labels) > 60
    unmapped = sorted(l for l in labels if surfaces.surface_for(l).name == 'system' and l not in SYSTEM_LABELS)
    assert not unmapped, unmapped
    for label in SYSTEM_LABELS:
        assert surfaces.surface_for(label).exempt


def test_every_registered_event_type_has_a_surface():
    from chatter_event_registry import EVENT_REGISTRY
    system_events = {'bot_backstory_regen', 'bot_tone_regen'}
    missing = sorted(e for e in EVENT_REGISTRY
                     if surfaces.surface_for('', e).name == 'system' and e not in system_events)
    assert not missing, missing
    assert surfaces.surface_for('', 'player_general_msg').name == 'general_reactive'


def test_surface_audiences_never_leak_party_facts_to_public_channels():
    for policy in surfaces.SURFACES.values():
        if policy.channel in ('general', 'say', 'guild'):
            assert policy.audience == 'public', policy.name
        if policy.channel == 'whisper':
            assert policy.audience == 'private'


def test_rollback_and_switch_make_engine_inert():
    prompt = 'Say something about the weather.'
    for config in ({**ON, 'LLMChatter.Dialogue.Pipeline': '1'}, {**ON, 'LLMChatter.Surfaces.Enable': '0'},
                   {**ON, 'LLMChatter.ChatterMode': 'roleplay'}):
        assert surfaces.configure(config) is False
        assert surfaces.apply(prompt, 'reaction_combat', config) is prompt
        assert surfaces.admit_event({'event_type': 'proximity_reply', 'extra_data': {}}) == (True, 'disabled')
    surfaces.configure(ON)


def test_handled_and_system_surfaces_unchanged():
    for label in ('reactive_reply', 'bot_player_whisper', 'general_player_msg', 'memory_generation', 'bot_tone'):
        prompt = PromptParts('user', 'system')
        assert surfaces.apply(prompt, label, ON) is prompt


def test_rules_appended_preserving_prompt_parts():
    combat = surfaces.apply(PromptParts('You took a hit.', 'Reply as JSON.'), 'reaction_combat', ON)
    assert combat.user_prompt == 'You took a hit.'
    assert combat.system_prompt.startswith('Reply as JSON.')
    assert 'SURFACE (combat' in combat.system_prompt and 'Mid-fight' in combat.system_prompt
    plain = surfaces.apply('A guildmate logged in.', 'guild_login_greeting', ON)
    assert isinstance(plain, PromptParts) and plain.user_prompt == 'A guildmate logged in.'
    general = surfaces.apply('x', 'ambient_conv', ON)
    assert 'public channel' in general.system_prompt


class FakeKnowledge:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def lookup(self, intents, world=None, deadline=None):
        self.calls.append((intents, world))
        return list(self.evidence), {}


@pytest.fixture
def knowledge(monkeypatch):
    import chatter_knowledge
    fake = FakeKnowledge([
        Evidence('quest', 'server_data', 'found', {'id': 783, 'title': 'A Threat Within'}),
        Evidence('npc_service', 'server_data', 'found', {'npc': 'Innkeeper'}, audience='party'),
    ])
    monkeypatch.setattr(chatter_knowledge, '_engine', fake)
    return fake


def test_quest_event_gets_exact_id_knowledge(knowledge):
    event = {'id': 1, 'event_type': 'bot_group_quest_accept',
             'extra_data': {'quest_id': 783, 'quest_name': 'A Threat Within', 'group_id': 5}}
    meta = {}
    with surfaces.dispatch_context(None, event):
        prompt = surfaces.apply(PromptParts('React to the quest.', 'JSON'), 'reaction_quest_accept', ON, meta)
    intent = knowledge.calls[0][0][0]
    assert (intent.kind, intent.entity_id) == ('quest_info', 783)
    assert 'DATA:' in prompt.user_prompt and 'A Threat Within' in prompt.user_prompt
    assert 'untrusted quoted data' in prompt.system_prompt
    assert meta['surface'] == 'quest'


def test_public_surface_filters_party_only_evidence(knowledge):
    event = {'id': 2, 'event_type': 'guild_player_message',
             'extra_data': {'player_message': 'who starts |cffffff00|Hquest:783:1|h[A Threat Within]|h|r ?',
                            'guild_id': 9, 'player_guid': 1}}
    with surfaces.dispatch_context(None, event):
        prompt = surfaces.apply('Reply to your guildmate.', 'guild_player_message', ON)
    assert 'A Threat Within' in prompt.user_prompt
    assert 'Innkeeper' not in prompt.user_prompt


def test_no_intent_means_no_lookup(knowledge):
    event = {'id': 3, 'event_type': 'guild_player_message',
             'extra_data': {'player_message': 'lol nice', 'guild_id': 9}}
    with surfaces.dispatch_context(None, event):
        prompt = surfaces.apply('Reply.', 'guild_player_message', ON)
    assert not knowledge.calls and 'DATA:' not in prompt


def test_knowledge_switch_off(knowledge):
    event = {'id': 4, 'event_type': 'bot_group_quest_complete', 'extra_data': {'quest_id': 783}}
    with surfaces.dispatch_context(None, event):
        surfaces.apply('x', 'reaction_quest_complete', {**ON, 'LLMChatter.Knowledge.Enable': '0'})
    assert not knowledge.calls


def test_rss_engine_surface_uses_trusted_speakers(monkeypatch):
    import chatter_relationships
    import chatter_rss
    seen = {}

    def fake_select(config, speakers, label):
        seen.update(label=label, speakers=speakers)
        return None
    monkeypatch.setattr(chatter_rss, 'select_topic', fake_select)
    scope = SimpleNamespace(trusted_speakers={11: 'Aldric', 12: 'Bryn'})
    token = chatter_relationships._scope.set(scope)
    try:
        surfaces.apply('Idle party chat.', 'group_idle_conv', ON)
    finally:
        chatter_relationships._scope.reset(token)
    assert seen['label'] == 'group_idle_conv'
    assert {s['guid'] for s in seen['speakers']} == {11, 12}
    assert 'group_idle_conv' in chatter_rss.LABELS
    # Combat never asks for news.
    seen.clear()
    surfaces.apply('x', 'reaction_combat', ON)
    assert not seen


# -- lifecycle ------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_life(**kw):
    clock = Clock()
    return chatter_lifecycle.Lifecycle(clock=clock, **kw), clock


def test_player_precedence_blocks_initiation_then_expires():
    life, clock = make_life(player_quiet=30, initiation_gap=0)
    life.note_player('party:5')
    assert life.admit_initiation('party:5') == (False, 'player_precedence')
    assert life.admit_initiation('party:6')[0]
    clock.now += 31
    assert life.admit_initiation('party:5')[0]


def test_conversation_window_and_personality_gap():
    life, clock = make_life(initiation_gap=20, conversation_seconds=40)
    assert life.admit_initiation('general:12', conversation=True, talkativeness=100)[0]
    clock.now += 15
    assert life.admit_initiation('general:12') == (False, 'conversation_active')
    clock.now += 30
    assert life.admit_initiation('general:12', talkativeness=0)[0]
    clock.now += 21  # quiet speaker -> 1.5x gap (30 s)
    assert life.admit_initiation('general:12') == (False, 'initiation_gap')
    clock.now += 10
    assert life.admit_initiation('general:12')[0]


def test_bot_reply_chain_ends_and_player_resets_it():
    life, clock = make_life(max_chain=3, chain_window=60, player_quiet=0)
    assert [life.admit_reply('say:1:2')[0] for _ in range(4)] == [True, True, True, False]
    assert life.admit_reply('say:1:2') == (False, 'chain_limit')
    life.note_player('say:1:2')
    assert life.admit_reply('say:1:2')[0]
    clock.now += 61
    assert all(life.admit_reply('say:1:2')[0] for _ in range(3))


def test_player_mid_conversation_blocks_new_bot_thread():
    life, _ = make_life(player_quiet=30)
    life.note_player('say:1:2')
    assert life.admit_reply('say:1:2') == (False, 'player_precedence')


def test_repeat_detection_and_bounds():
    life, _ = make_life(max_scopes=3)
    assert not life.is_repeat('general:1', 'Anyone selling linen cloth in Goldshire?')
    assert life.is_repeat('general:1', 'anyone selling linen cloth goldshire')
    assert not life.is_repeat('general:2', 'anyone selling linen cloth goldshire')
    assert not life.is_repeat('general:1', 'ok')  # too short to judge
    for i in range(10):
        life.note_player(f'party:{i}')
    assert len(life.scopes) == 3


def test_admit_event_routes_policies():
    surfaces.chatter_lifecycle._registry = chatter_lifecycle.Lifecycle(max_chain=2, player_quiet=30)
    reply = {'event_type': 'proximity_reply', 'extra_data': {'zone_id': 12, 'area_id': 87}}
    assert surfaces.admit_event(reply)[0] and surfaces.admit_event(reply)[0]
    assert surfaces.admit_event(reply) == (False, 'chain_limit')
    player = {'event_type': 'proximity_player_say', 'extra_data': {'zone_id': 12, 'area_id': 87}}
    assert surfaces.admit_event(player) == (True, 'player')
    idle = {'event_type': 'guild_idle_chatter', 'extra_data': {'guild_id': 4}}
    assert surfaces.admit_event({'event_type': 'guild_player_message', 'extra_data': {'guild_id': 4}})[0]
    assert surfaces.admit_event(idle) == (False, 'player_precedence')
    combat = {'event_type': 'bot_group_combat', 'extra_data': {'group_id': 4}}
    assert surfaces.admit_event(combat) == (True, 'event')


def test_admit_line_drops_repeated_opener_as_a_whole():
    surfaces.chatter_lifecycle._registry = chatter_lifecycle.Lifecycle()
    event = {'event_type': 'proximity_conversation', 'extra_data': {'zone_id': 12, 'area_id': 3}}
    with surfaces.dispatch_context(None, event):
        assert surfaces.admit_line('The murlocs by the lake are getting bold again', 0)
        assert surfaces.admit_line('Someone should thin them out before dusk', 1)
    with surfaces.dispatch_context(None, event):
        assert not surfaces.admit_line('murlocs by the lake getting bold again', 0)
        assert not surfaces.admit_line('A completely different second line', 1)
    reactive = {'event_type': 'bot_group_player_msg', 'extra_data': {'group_id': 3}}
    for _ in range(2):
        with surfaces.dispatch_context(None, reactive):
            assert surfaces.admit_line('Heading to the inn now, follow me', 0)


def test_initiation_gate_returns_quiet_without_calling_builder():
    surfaces.chatter_lifecycle._registry = chatter_lifecycle.Lifecycle(player_quiet=30)
    calls = []

    @surfaces.initiation_gate('general')
    def process_statement(db, cursor, client, config, request, bot):
        calls.append(surfaces._dispatch.get().scope)
        return True

    @surfaces.initiation_gate('party')
    def _idle_single_statement(db, client, config, group_id, all_bots, mode, chat_hist, members, now,
                               zone_id=0):
        calls.append(group_id)
        return True

    bot = {'guid': 1, 'name': 'Aldric'}
    assert process_statement(None, None, None, {}, {'zone_id': 12}, bot) is True
    assert calls == ['general:12']
    surfaces.chatter_lifecycle._registry.note_player('general:12')
    assert process_statement(None, None, None, {}, {'zone_id': 12}, bot) is True
    surfaces.chatter_lifecycle._registry.note_player('party:7')
    assert _idle_single_statement(None, None, {}, 7, [bot], 'normal', '', [], 0) is False
    assert calls == ['general:12']


SOURCE = {name: (TOOLS / name).read_text(encoding='utf-8') for name in (
    'chatter_llm.py', 'llm_chatter_bridge.py', 'chatter_ambient.py', 'chatter_group.py', 'chatter_db.py')}


def test_integration_points():
    llm = SOURCE['chatter_llm.py']
    assert llm.index('apply_surface(prompt, label, config, metadata)') < llm.index('enrich_prompt(prompt, label)')
    bridge = SOURCE['llm_chatter_bridge.py']
    assert 'configure_surfaces(config)' in bridge
    body = bridge[bridge.index('def process_single_event'):]
    assert body.index('admit_event(event)') < body.index('poll_deliveries(db)')
    assert 'with dispatch_context(db, event), event_context(db, config, event):' in body
    for name, fn in (('chatter_ambient.py', 'process_statement'), ('chatter_ambient.py', 'process_conversation'),
                     ('chatter_group.py', '_idle_single_statement'), ('chatter_group.py', '_idle_conversation')):
        assert re.search(r"@initiation_gate\('(general|party)'\)\ndef " + fn + r'\(', SOURCE[name]), fn
    db = SOURCE['chatter_db.py']
    insert = db[db.index('def insert_chat_message'):]
    assert insert.index('admit_line(message, sequence, channel)') < insert.index('reserve_party_slot(')


def test_engine_adds_no_io_on_the_world_thread():
    text = (TOOLS / 'chatter_surfaces.py').read_text(encoding='utf-8') + \
        (TOOLS / 'chatter_lifecycle.py').read_text(encoding='utf-8')
    for forbidden in ('cursor.execute', 'requests.', 'urlopen', 'time.sleep'):
        assert forbidden not in text


def test_general_social_layer():
    from chatter_social import decide_general
    q = 'anyone know where the wool drops?'
    history = [{'is_bot': False, 'speaker_guid': 5, 'message': q}]
    assert decide_general(q, history, 5).respond
    moved = history + [{'is_bot': False, 'speaker_guid': 6, 'message': 'lfg deadmines'}]
    assert decide_general(q, moved, 5).reason == 'conversation_moved_on'
    answered = [{'is_bot': False, 'speaker_guid': 5, 'message': q},
                {'is_bot': True, 'speaker_guid': 70, 'message': 'humanoids in westfall'},
                {'is_bot': False, 'speaker_guid': 5, 'message': q}]
    assert decide_general(q, answered, 5).reason == 'already_answered'
    assert decide_general('who mining', [], 5).reason == 'playerbot_command'
    general = (TOOLS / 'chatter_general.py').read_text(encoding='utf-8')
    body = general[general.index('def process_general_player_msg_event'):]
    assert body.index('decide_general(') < body.index('_select_primary_bot(')


def test_custom_races_keep_their_faction():
    """Staging finding: Worgen/Goblin (mod-worgoblin) bots were filtered out."""
    from chatter_constants import RACE_NAMES, race_sql_filter
    from chatter_group import _filter_group_bots_by_player_faction
    from chatter_shared import get_race_faction
    assert get_race_faction(12) == 'Alliance' and get_race_faction(9) == 'Horde'
    assert RACE_NAMES[12] == 'Worgen' and RACE_NAMES[9] == 'Goblin'
    assert '12' in race_sql_filter('Alliance') and '9' in race_sql_filter('Horde')
    assert race_sql_filter('') == ''
    bots = [{'bot_guid': 1, 'faction_race': 12}, {'bot_guid': 2, 'faction_race': 9}]
    assert [b['bot_guid'] for b in _filter_group_bots_by_player_faction(bots, 3)] == [1]
    assert [b['bot_guid'] for b in _filter_group_bots_by_player_faction(bots, 2)] == [2]
    for name in ('chatter_db.py', 'chatter_general.py', 'chatter_guild.py', 'chatter_battlegrounds.py'):
        text = (TOOLS / name).read_text(encoding='utf-8')
        assert '(1, 3, 4, 7, 11)' not in text and '(2, 5, 6, 8, 10)' not in text, name


def test_live_claims_need_evidence():
    """Staging finding: a warrior's "mana sec" went out with only an alive claim."""
    from chatter_dialogue import unsupported_claim
    low = Evidence('live_self', 'observed', 'found', {'power': 'mana', 'power_pct': 12, 'power_low': True},
                   audience='party', observer=7)
    ok = Evidence('live_self', 'observed', 'found', {'power': 'mana', 'power_pct': 90, 'power_low': False},
                  audience='party', observer=7)
    alive = Evidence('live_self', 'observed', 'found', {'alive': True}, audience='party', observer=7)
    assert unsupported_claim('mana sec', [low], 7) == ''
    assert unsupported_claim('mana sec', [ok], 7) == 'unsupported_power_claim'
    assert unsupported_claim('oom, one sec', [alive], 7) == 'unsupported_power_claim'
    assert unsupported_claim('mana sec', [low], 8) == 'unsupported_power_claim'  # someone else's mana
    assert unsupported_claim('yeah I see it, by the tower', [], 7) == 'unsupported_sighting_claim'
    assert unsupported_claim('hey, whats up', [], 7) == ''
    rare = Evidence('rare_seen', 'observed', 'found', {'spawn': 1, 'entry': 2, 'name': 'Mor'}, observer=7)
    assert unsupported_claim("it's right here", [rare], 7) == ''
