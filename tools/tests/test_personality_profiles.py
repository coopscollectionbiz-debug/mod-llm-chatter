"""Offline personality, prompt routing and silence regression tests."""

import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Same optional SDK stubs used by existing offline prompt tests.
import test_normal_mode_player_chat  # noqa: F401,E402
import chatter_personality as personalities  # noqa: E402
from chatter_mode import build_player_prompt_header, resolve_player_personality  # noqa: E402
from chatter_personality import ProfileStore, generate_profile, profile_for  # noqa: E402


@pytest.fixture
def service(tmp_path):
    with patch.object(personalities, '_store', None), patch.object(personalities, '_lookup', None):
        lookup = Mock(side_effect=lambda name: {'Quiet': 100, 'Chatty': 200}[name])
        personalities.configure({'LLMChatter.Personality.Path': str(tmp_path / 'profiles.sqlite3')}, lookup)
        yield personalities._store, lookup
    personalities._names.clear()


def test_persistent_across_process_restart(tmp_path):
    path = tmp_path / 'profiles.sqlite3'
    first = ProfileStore(path).get(123)
    script = Path(personalities.__file__)
    output = subprocess.check_output([sys.executable, str(script), str(path), '123'], text=True)
    assert json.loads(output) == first


def test_guid_identity_survives_name_and_channel_changes(service):
    first = profile_for('Quiet', 100)
    assert first == profile_for('Renamed', 100)
    for channel in ('general', 'party', 'raid', 'guild', 'say', 'battleground'):
        prompt = build_player_prompt_header('Quiet', mode='normal', channel=channel)
        assert personalities.profile_instruction('Quiet', 100) in prompt
    assert service[1].call_count == 0


def test_lazy_creation_and_cached_name_lookup(service):
    store, lookup = service
    assert not store.path.exists()
    assert profile_for('Quiet') == profile_for('Quiet')
    assert lookup.call_count == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM profiles').fetchone()[0] == 1


def test_warm_profile_lookups_never_open_database(service):
    store, lookup = service
    profile_for('Quiet', 100)
    with patch.object(store, '_connect', side_effect=AssertionError('warm DB access')):
        for _ in range(100):
            assert profile_for('Quiet')['guid'] == 100
    lookup.assert_not_called()


def test_precache_prompt_uses_profile_instead_of_legacy_tone(service):
    from chatter_group_prompts import build_precache_combat_pull_prompt
    prompt = build_precache_combat_pull_prompt(
        'Quiet', 'Human', 'Warrior', 20, ['fel-scarred'], 'neutral',
        stored_tone='heroic and prayer-muttering', mode='normal',
    )
    assert 'fel-scarred' not in prompt
    assert 'prayer-muttering' not in prompt
    assert 'PLAYER PROFILE Quiet' in prompt
    assert len(personalities.profile_instruction('Quiet', 100)) < 700


def test_diversity_and_independent_typing_styles():
    profiles = [generate_profile(g) for g in range(1, 501)]
    assert len({p['archetype'] for p in profiles}) == len(personalities.ARCHETYPES)
    competitive = [p for p in profiles if p['archetype'] == 'competitive']
    assert len({p['message_length'] for p in competitive}) == 3
    assert len({p['capitalization'] for p in competitive}) == 3
    assert any(p['helpfulness'] < 25 for p in profiles)


def test_version_reset_and_edit_do_not_touch_memory(tmp_path):
    path = tmp_path / 'state.sqlite3'
    store = ProfileStore(path)
    before = store.get(123)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE memories (message TEXT)')
        db.execute("INSERT INTO memories VALUES ('finished a dungeon together')")
    store.edit(123, {'talkativeness': 2, 'capitalization': 'lowercase'})
    changed = ProfileStore(path, version=2)
    assert changed.get(123)['version'] == 2
    assert changed.get(123)['talkativeness'] == 2
    changed.edit(123)
    assert changed.get(123) == generate_profile(123, version=2)
    assert changed.get(123) != before
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT message FROM memories').fetchone()[0] == 'finished a dungeon together'


def test_edits_validate_and_cached_values_are_not_mutable(tmp_path):
    store = ProfileStore(tmp_path / 'profiles.sqlite3', cache_size=1)
    first = store.get(1)
    first['gameplay_interests'].append('bad value')
    assert 'bad value' not in store.get(1)['gameplay_interests']
    for values in ({'talkativeness': 101}, {'tone': 'heroic'}, {'punctuation': 'nonsense'}):
        with pytest.raises(ValueError):
            store.edit(1, values)
    store.get(2)
    assert len(store.cache) == 1
    assert store.get(1) == generate_profile(1)


def test_weight_configuration_is_deterministic():
    weights = {key: int(key == 'quiet') for key in personalities.ARCHETYPES}
    assert all(generate_profile(g, weights=weights)['archetype'] == 'quiet' for g in range(1, 30))
    with pytest.raises(ValueError):
        generate_profile(1, weights={key: -1 for key in weights})


def test_dist_config_and_quoted_values_are_accepted(tmp_path):
    from chatter_shared import parse_config
    config = parse_config(str(Path(__file__).resolve().parents[2] / 'conf' / 'mod_llm_chatter.conf.dist'))
    config['LLMChatter.Personality.Path'] = '"' + str(tmp_path / 'profiles.sqlite3') + '"'
    with patch.object(personalities, '_store', None), patch.object(personalities, '_lookup', None):
        personalities.configure(config, lambda name: 123)
        assert personalities._store.seed == 'azeroth'
        assert not personalities._store.path.exists()
        assert profile_for('Quiet') == generate_profile(123)
    personalities._names.clear()


def test_concurrent_lazy_creation_has_one_row(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = ProfileStore(tmp_path / 'profiles.sqlite3')
    with ThreadPoolExecutor(max_workers=8) as executor:
        profiles = list(executor.map(store.get, [123] * 32))
    assert all(profile == profiles[0] for profile in profiles)
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM profiles').fetchone()[0] == 1


def test_normal_profiles_ignore_rp_traits_and_preserve_roleplay(service):
    traits, tone = resolve_player_personality('Quiet', ['fel-scarred', 'prayer-muttering'], 'heroic')
    assert 'fel-scarred' not in traits and 'heroic' not in tone
    assert resolve_player_personality('Quiet', ['fel-scarred'], 'heroic', 'roleplay') == (['fel-scarred'], 'heroic')


def test_quiet_bots_speak_less_and_urgent_notifications_survive():
    quiet = dict(generate_profile(1), talkativeness=5, initiation=30)
    chatty = dict(quiet, talkativeness=90, initiation=90)
    quiet_rng, chatty_rng = random.Random(42), random.Random(42)
    quiet_count = sum(personalities.should_speak(quiet, rng=quiet_rng.random) for _ in range(10000))
    chatty_count = sum(personalities.should_speak(chatty, rng=chatty_rng.random) for _ in range(10000))
    assert quiet_count < chatty_count / 5
    assert personalities.should_speak(quiet, important=True, rng=lambda: 1)
    assert personalities.should_speak(quiet, directly_addressed=True, rng=lambda: 0.5)


def test_ambient_silence_consumes_request_without_api_or_db_work(service):
    import chatter_ambient
    db, cursor, client = Mock(), Mock(), Mock()
    with (
        patch.object(personalities, 'should_speak', return_value=False),
        patch.object(chatter_ambient, 'call_llm') as llm,
    ):
        assert chatter_ambient.process_statement(db, cursor, client, {}, {'id': 1},
                                                {'name': 'Quiet', 'guid': 100}) is True
        llm.assert_not_called()
        db.assert_not_called()
        cursor.execute.assert_not_called()


def test_multi_bot_prompt_uses_distinct_profiles_and_keeps_system_schema(service):
    from chatter_prompts import build_plain_conversation_prompt
    from chatter_shared import PromptParts
    bots = [{'name': n, 'guid': g, 'race': 'Human', 'class': 'Warrior', 'level': 20, 'zone': 'Westfall'}
            for n, g in [('Quiet', 100), ('Chatty', 200)]]
    prompt = build_plain_conversation_prompt(bots, config={'LLMChatter.ChatterMode': 'normal'})
    assert isinstance(prompt, PromptParts)
    for bot in bots:
        assert personalities.profile_instruction(bot['name'], bot['guid']) in prompt.user_prompt
    assert 'EXACTLY' not in prompt.system_prompt
    assert 'JSON' in prompt.system_prompt
    assert 'early endings' in prompt.user_prompt
    assert service[1].call_count == 0


def test_general_reply_never_uses_rp_or_forced_names(service):
    from chatter_general import _build_general_response_prompt
    prompt = _build_general_response_prompt('Quiet', 'Human', 'Warrior', 20, '',
                                           ['fel-scarred'], 'Tester', 'hi', 'Westfall', '', 'normal')
    assert 'fel-scarred' not in prompt
    assert 'Address Tester by name' not in prompt
    assert 'Creative twist:' not in prompt
    assert personalities.profile_instruction('Quiet', 100) in prompt


def test_normal_guild_early_ending_is_valid_without_all_speakers():
    from chatter_guild import _valid_guild_conversation
    messages = [{'name': 'Quiet', 'message': 'nah'}]
    assert _valid_guild_conversation(messages, ['Quiet', 'Chatty'], 'normal')
    assert not _valid_guild_conversation(messages, ['Quiet', 'Chatty'], 'roleplay')
    assert not _valid_guild_conversation([], ['Quiet', 'Chatty'], 'normal')
    assert not _valid_guild_conversation([{'name': 'Stranger'}], ['Quiet'], 'normal')


def test_gameplay_reaction_retains_event_and_profile(service):
    from chatter_group_prompts import build_kill_reaction_prompt, build_low_health_callout_prompt
    bot = {'name': 'Quiet', 'guid': 100, 'race': 'Human', 'class': 'Warrior', 'level': 20}
    prompt = build_kill_reaction_prompt(bot, ['fel-scarred'], 'SuppliedBoss', True, False, 'normal')
    assert 'SuppliedBoss' in prompt and 'PLAYER PROFILE Quiet' in prompt
    assert 'fel-scarred' not in prompt
    callout = build_low_health_callout_prompt(bot, ['fel-scarred'], 'Tester', 'normal')
    assert 'Tester' in callout and 'PLAYER PROFILE Quiet' in callout


def test_quality_evaluator_reports_requested_indicators():
    from evaluate_dialogue import evaluate
    report = evaluate([{'label': 'guild_idle_chatter', 'response': json.dumps([
        {'speaker': 'Quiet', 'message': 'yeah same?'},
        {'speaker': 'Chatty', 'message': 'Happy to help Quiet'},
    ])}])
    assert report['messages'] == 2
    assert report['by_label']['guild_idle_chatter']['question_rate'] == 0.5
    assert report['by_label']['guild_idle_chatter']['assistant_phrase_rate'] == 0.5
    assert report['multi_speaker_agreement_opening_rate'] == 0.5
