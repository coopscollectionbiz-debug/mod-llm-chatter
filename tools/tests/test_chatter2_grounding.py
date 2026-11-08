"""Grounding guards added after the real-model evaluation (gpt-4o-mini, 42 requests).

Cases are the actual failing replies from that run, plus the legitimate
replies that must keep passing.
"""

from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import chatter_surfaces as surfaces  # noqa: E402
from chatter_evidence import Evidence, render_block  # noqa: E402
from chatter_shared import PromptParts  # noqa: E402

ON = {'LLMChatter.Dialogue.Pipeline': '2', 'LLMChatter.Surfaces.Enable': '1', 'LLMChatter.ChatterMode': 'normal'}


@pytest.fixture(autouse=True)
def engine_on():
    surfaces.configure(ON)
    yield
    surfaces.configure({'LLMChatter.Surfaces.Enable': '0'})


def asked(text, event_type='bot_group_player_msg'):
    return surfaces.dispatch_context(None, {'event_type': event_type,
                                            'extra_data': {'player_message': text, 'group_id': 1}})


def loot_data(status='found'):
    block, _ = render_block([Evidence('loot', 'server_data', status, {'item': 'Linen Cloth', 'sources': ['Defias']})],
                            'party')
    return 'DATA:\n' + block


@pytest.mark.parametrize('reply', [
    "i'm at 70% mana, focusing on healing.",
    "i'm at 60% mana, focusing on healing.",
    'health at 30%, careful',
])
def test_invented_self_stat_rejected_on_every_surface(reply):
    for label in ('reaction_combat', 'raid_battle_cry', 'reactive_reply', 'group_idle_conv'):
        assert surfaces.check_reply('{"message": "%s"}' % reply, 'You took a hit.', label) == 'invented_self_stat'


def test_self_stat_allowed_when_prompt_gives_it():
    assert surfaces.check_reply('at 45% health, careful', 'You are critically wounded (45% health).',
                                'reaction_low_health') == ''
    assert surfaces.check_reply('22% mana, give me a sec', 'DATA:\n{"kind":"live_self","power_pct":22}',
                                'reactive_reply') == ''
    assert surfaces.check_reply('at 45% health', 'You are wounded (30% health).', 'reaction_low_health') \
        == 'invented_self_stat'
    # Not a self-stat: progress and percentages of other things.
    assert surfaces.check_reply('boss is at 20%, push', 'Pull now.', 'raid_battle_cry') == ''


@pytest.mark.parametrize('question,reply', [
    ('where does linen cloth drop', 'i think it drops from humanoids, like those in westfall or the barrens.'),
    ('where does linen cloth drop', 'you can get linen cloth from humanoid mobs in starting areas, like westfall.'),
    ('where does linen cloth drop', 'you can get linen cloth from killing low-level humanoids, like gnolls or murlocs.'),
    ('anyone know where the wool drops?', 'i think wool drops from sheep in the wetland area.'),
    ('anyone know where the wool drops?', 'might be from sheep in westfall or the hillsbrad foothills.'),
    ('who sells mana potions?', 'the vendor in goldshire sells them'),
])
def test_guessed_sources_rejected_without_data(question, reply):
    for event in ('bot_group_player_msg', 'player_general_msg', 'bot_player_whisper', 'guild_player_message'):
        with asked(question, event):
            assert surfaces.check_reply(reply, 'Reply to the player.', 'reactive_reply') == 'ungrounded_source'


def test_sources_allowed_with_verified_data_or_honest_uncertainty():
    with asked('where does linen cloth drop'):
        assert surfaces.check_reply('drops from the defias in westfall', loot_data(), 'reactive_reply') == ''
        assert surfaces.check_reply('drops from the defias', loot_data('partial'), 'reactive_reply') == ''
        # not_found / unavailable records are not a source.
        assert surfaces.check_reply('drops from the defias', loot_data('not_found'), 'reactive_reply') \
            == 'ungrounded_source'
        assert surfaces.check_reply('not sure tbh, maybe check the AH', 'Reply.', 'reactive_reply') == ''
    # No source question: describing a kill is fine.
    with asked('nice pull'):
        assert surfaces.check_reply('that dropped from vancleef, gg', 'Reply.', 'reactive_reply') == ''


def test_source_rule_added_to_prompts_including_reactive():
    with asked('where does linen cloth drop'):
        reactive = surfaces.apply(PromptParts('user', 'sys'), 'reactive_reply', ON)
        assert reactive.user_prompt == 'user' and 'SOURCE RULE' in reactive.system_prompt
        guild = surfaces.apply('Reply.', 'guild_player_message', ON)
        assert 'SOURCE RULE' in guild.system_prompt
        sourced = surfaces.apply(PromptParts('user\n' + loot_data(), 'sys'), 'reactive_reply', ON)
        assert 'SOURCE RULE' not in sourced.system_prompt
    with asked('heals ready?'):
        prompt = PromptParts('user', 'sys')
        assert surfaces.apply(prompt, 'reactive_reply', ON) is prompt


def test_guards_inert_on_rollback_and_system_labels():
    surfaces.configure({**ON, 'LLMChatter.Dialogue.Pipeline': '1'})
    assert surfaces.check_reply("i'm at 70% mana", 'x', 'reaction_combat') == ''
    surfaces.configure(ON)
    assert surfaces.check_reply("i'm at 70% mana", 'x', 'memory_generation') == ''


def test_call_llm_discards_rejected_reply(monkeypatch):
    import chatter_llm
    monkeypatch.setattr(chatter_llm, '_breaker', chatter_llm.CircuitBreaker())
    client = MagicMock()
    client.messages.create.return_value = MagicMock(content=[MagicMock(text='{"message": "im at 70% mana"}')])
    meta = {}
    config = {**ON, 'LLMChatter.Provider': 'anthropic'}
    assert chatter_llm.call_llm(client, 'You were hit.', config, label='reaction_combat', metadata=meta) is None
    assert meta['rejected'] == 'invented_self_stat'
    client.messages.create.return_value = MagicMock(content=[MagicMock(text='{"message": "on it"}')])
    assert chatter_llm.call_llm(client, 'You were hit.', config, label='reaction_combat') == '{"message": "on it"}'


@pytest.mark.parametrize('reply,prompt,expected', [
    ("i'm at half health, keeping up the heals.", 'Your raid is pulling Onyxia now.', 'invented_self_stat'),
    ("im at half health, keep the heals coming.", 'Your raid is pulling Onyxia now.', 'invented_self_stat'),
    ("i'm low on mana, one sec", 'Pull now.', 'invented_self_stat'),  # self-state, nothing in prompt
    ('keep focus, watching my health', 'Pull now.', ''),
    ("keeping an eye on everyone's health.", 'Pull now.', ''),
    ("i'm at half health", 'You are critically wounded (45% health).', ''),
    ('yeah, just need some mana back', 'DATA:\n{"kind":"live_self","power_pct":22}', ''),
])
def test_word_form_self_state(reply, prompt, expected):
    """Focused re-run finding: "i'm at half health" with no health in the prompt (2/3)."""
    assert surfaces.check_reply(reply, prompt, 'raid_battle_cry') == expected


def test_typographic_apostrophe_is_normalised():
    assert surfaces.check_reply('{"message": "i’m at half health"}', 'Pull now.', 'raid_battle_cry') \
        == 'invented_self_stat'
