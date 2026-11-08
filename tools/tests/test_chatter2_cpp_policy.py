"""Chatter 2.0 C++ policy execution and world-thread source contracts.

The header-only claim and route policies are compiled and executed
standalone. Behaviour that needs a worldserver (grid searches, packet
sends) is covered by source contracts: bounded capture, filtering before
side effects, revalidation before send, and no blocking database work.
"""

from pathlib import Path
import os
import re
import subprocess
import sys

import pytest

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parents[1] / 'src'
sys.path.insert(0, str(TESTS))
from test_delivery_performance import _find_msvc  # noqa: E402


def _read(name):
    return (SRC / name).read_text(encoding='utf-8')


def _body(text, start, end):
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))]


def build(source, tmp_path):
    exe = tmp_path / ('policy.exe' if os.name == 'nt' else 'policy')
    import shutil
    gnu = os.environ.get('LLMCHATTER_CXX') or shutil.which('g++') or shutil.which('clang++')
    if gnu:
        return exe, subprocess.run([gnu, '-std=c++17', '-Wall', '-Wextra', '-Werror', f'-I{SRC}', str(source),
                                    '-o', str(exe)], capture_output=True, text=True)
    vcvars = _find_msvc() if os.name == 'nt' else None
    if not vcvars:
        return None, None
    script = tmp_path / 'build.bat'
    script.write_text('@echo off\r\n' f'call "{vcvars}" >nul\r\n'
                      f'cl /nologo /EHsc /std:c++17 /W4 /WX /I"{SRC}" "{source}" /Fe"{exe}" /Fo"{tmp_path}\\\\"\r\n',
                      encoding='utf-8')
    return exe, subprocess.run(['cmd', '/c', str(script)], capture_output=True, text=True)


def test_claim_and_route_policies_execute(tmp_path):
    exe, result = build(TESTS / 'cpp' / 'test_chatter2_policy.cpp', tmp_path)
    if result is None:
        pytest.skip('no C++ compiler available')
    assert result.returncode == 0, result.stdout + result.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout
    assert 'all checks passed' in run.stdout


def test_claim_policy_header_is_game_free():
    includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', _read('LLMChatterClaimPolicy.h'))
    assert set(includes) <= {'cstdint', 'string', 'string_view', 'vector'}


def test_whisper_ingress_filters_commands_before_any_side_effect():
    body = _body(_read('LLMChatterWhisper.cpp'), 'void HandlePlayerWhisperToBot(', '\n}\n')
    filters = [body.index(token) for token in (
        "text[0] == '@'", 'IsLikelyPlayerbotControlCommand(text)', 'IsPlayerChatPrefixIgnored(text)')]
    effects = [body.index(token) for token in ('CoolingDown(', 'StoreReactiveChat(', 'QueueChatterEvent(')]
    assert max(filters) < min(effects)
    # Never consumes the whisper: Playerbots keeps receiving it.
    hook = _body(_read('LLMChatterPlayer.cpp'), 'Player* receiver) override', '\n    }\n')
    assert 'return true;' in hook and 'return false' not in hook
    assert 'LANG_ADDON' in hook
    assert 'PLAYERHOOK_CAN_PLAYER_USE_PRIVATE_CHAT' in _read('LLMChatterPlayer.cpp')


def test_whisper_audience_is_exact_pair_and_rechecked_at_send():
    whisper = _read('LLMChatterWhisper.cpp')
    assert '"whisper:{}:{}:{}", ReactiveProcessEpochId()' in whisper
    delivery = _read('LLMChatterDelivery.cpp')
    branch = _body(delivery, 'if (reactiveWhisper)', 'else if (reactiveParty)')
    assert 'WhisperReactiveAudience(source, bot)' in branch
    assert 'HasIgnore(bot->GetGUID())' in branch
    assert branch.index('withinDeadline()') < branch.index('bot->Whisper(')


def test_live_claims_are_revalidated_before_every_reactive_send():
    delivery = _read('LLMChatterDelivery.cpp')
    reactive = _body(delivery, 'if (reactiveParty || reactiveGeneral || reactiveWhisper)',
                     'StoreReactiveChat(row.reactiveAudience, bot, true, message);')
    claim_check = reactive.index('ReactiveClaimsHold(row.claimJson, bot, claimReason)')
    for send in ('bot->Whisper(', 'SendPartyMessageInstant(', 'destination->Say('):
        assert claim_check < reactive.index(send), send


def test_live_capture_is_bounded_and_demand_driven():
    live = _read('LLMChatterLiveContext.cpp')
    capture = _body(live, 'std::string BuildReactiveLiveJson(', '\nbool ReactiveClaimsHold(')
    assert 'count >= 8' in capture and 'rares >= 4' in capture
    assert 'if (scanRares && sLLMChatterConfig->_liveRareScanRadius > 0)' in capture
    assert 'CanSeeOrDetect(creature)' in capture
    assert 'CharacterDatabase' not in live and 'WorldDatabase' not in live
    party = _read('LLMChatterGroupCombat.cpp')
    assert 'BuildReactiveLiveJson(player, reactiveBots, MessageMentionsRare(safeMsg))' in party


def test_radius_config_is_capped():
    config = _read('LLMChatterConfig.cpp')
    assert '"LLMChatter.Context.RareScanRadius", 80), 150)' in config
    assert '"LLMChatter.Whisper.Enable", false' in config


def test_migration_is_additive_and_idempotent():
    sql = (SRC.parent / 'data' / 'sql' / 'characters' / 'updates'
           / '20261108_chatter2_live_claims_whisper.sql').read_text(encoding='utf-8')
    assert 'information_schema.COLUMNS' in sql and "COLUMN_NAME = 'claim_json'" in sql
    assert "LIKE '%bot_player_whisper%'" in sql
    assert not re.search(r'\bDROP\s+(TABLE|COLUMN|INDEX|DATABASE)\b|\bDELETE\s+FROM\b|\bTRUNCATE\b', sql, re.I)
    # The rewritten enum keeps every value of the previous definition.
    previous = (SRC.parent / 'data' / 'sql' / 'characters' / 'updates'
                / '20260914_npc_multidirectional_interactions.sql').read_text(encoding='utf-8')
    old = set(re.findall(r"'([a-z_]+)'", previous.split('enum(', 1)[1].split(') NOT NULL', 1)[0]))
    new = set(re.findall(r"'([a-z_]+)'", sql.split('enum(', 1)[1].split(') NOT NULL', 1)[0]))
    assert old <= new and new - old == {'bot_player_whisper'}
