"""Playerbots 'who' collision: one case table, three implementations.

Playerbots' ExternalEventHelper::ParseChatCommand routes any "who ..."
sentence to WhoAction, which whispers "playing with <master>". The fix is
narrow: WhoAction declines a parameter whose first word continues a
sentence, and Chatter treats exactly those lines as conversation. Bare
"who", "who <skill>", item names and any item/quest link stay commands.
"""

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import pytest

TESTS = Path(__file__).resolve().parent
MODULE = TESTS.parents[1]
SRC = MODULE / 'src'
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))

import chatter_group  # noqa: E402
from chatter_social import decide_party  # noqa: E402

CASES = json.loads((TESTS / 'data' / 'who_routing_cases.json').read_text(encoding='utf-8'))['cases']
PLAYERBOTS_ACTIONS = Path(os.environ.get(
    'PLAYERBOTS_WHO_DIR', r'C:\Azerothcore-dev\mod-playerbots-who\src\Ai\Base\Actions'))


@pytest.mark.parametrize('case', CASES, ids=[c['text'][:40] for c in CASES])
def test_python_mirror_matches_table(case):
    assert chatter_group.is_conversational_who_question(case['text']) is case['conversational']
    assert chatter_group._is_playerbot_command(case['text']) is case['command']


def test_switch_off_restores_command_routing(monkeypatch):
    monkeypatch.setattr(chatter_group, '_conversational_who', False)
    assert chatter_group._is_playerbot_command('who starts this quest?') is True


def test_init_group_config_reads_switch():
    old = chatter_group._conversational_who
    try:
        chatter_group.init_group_config({'LLMChatter.PlayerChat.ConversationalWho': '0'})
        assert chatter_group._conversational_who is False
        chatter_group.init_group_config({})
        assert chatter_group._conversational_who is True
    finally:
        chatter_group._conversational_who = old


def test_social_layer_keeps_at_name_conversation_and_drops_selectors():
    bots = [{'bot_guid': 7, 'bot_name': 'Thrall', 'role': 'tank'}]
    assert decide_party('@heal follow', bots).reason == 'playerbot_command'
    assert decide_party('who mining', bots).reason == 'playerbot_command'
    assert decide_party('@Thrall how are you?', bots).reason != 'playerbot_command'
    assert decide_party('who starts this quest?', bots).reason != 'playerbot_command'


def test_source_contracts():
    group = (SRC / 'LLMChatterGroup.cpp').read_text(encoding='utf-8')
    body = group[group.index('IsLikelyPlayerbotControlCommand'):]
    check = body.index('IsConversationalWhoQuestion(msg)')
    assert check < body.index("msg.find(' ')"), 'who check must precede prefix matching'
    assert '_conversationalWhoEnable' in body[:check + 200]
    conf = (MODULE / 'conf' / 'mod_llm_chatter.conf.dist').read_text(encoding='utf-8')
    assert 'LLMChatter.PlayerChat.ConversationalWho = 1' in conf
    action = PLAYERBOTS_ACTIONS / 'WhoAction.cpp'
    if action.exists():
        text = action.read_text(encoding='utf-8')
        execute = text[text.index('bool WhoAction::Execute'):text.index('QueryTrade(std::string const text)')]
        early = execute.index('IsConversationalParam(text)')
        assert early < execute.index('bot->Whisper('), 'decline before any whisper'
        assert 'return false' in execute[early:early + 80]


def _compile(tmp_path):
    source = TESTS / 'cpp' / 'test_who_routing.cpp'
    exe = tmp_path / ('who.exe' if os.name == 'nt' else 'who')
    have_pb = (PLAYERBOTS_ACTIONS / 'WhoConversation.h').exists()
    gnu = os.environ.get('LLMCHATTER_CXX') or shutil.which('g++') or shutil.which('clang++')
    if gnu:
        cmd = [gnu, '-std=c++17', '-Wall', '-Wextra', '-Werror', f'-I{SRC}']
        if have_pb:
            cmd += [f'-I{PLAYERBOTS_ACTIONS}', '-DHAVE_PLAYERBOTS_WHO']
        return exe, have_pb, subprocess.run(cmd + [str(source), '-o', str(exe)], capture_output=True, text=True)
    from test_delivery_performance import _find_msvc
    vcvars = _find_msvc() if os.name == 'nt' else None
    if not vcvars:
        return None, have_pb, None
    extra = f' /I"{PLAYERBOTS_ACTIONS}" /DHAVE_PLAYERBOTS_WHO' if have_pb else ''
    script = tmp_path / 'build.bat'
    script.write_text('@echo off\r\n' f'call "{vcvars}" >nul\r\n'
                      f'cl /nologo /EHsc /std:c++17 /W4 /WX /I"{SRC}"{extra} "{source}" /Fe"{exe}" '
                      f'/Fo"{tmp_path}\\\\"\r\n', encoding='utf-8')
    return exe, have_pb, subprocess.run(['cmd', '/c', str(script)], capture_output=True, text=True)


def test_cpp_implementations_match_table(tmp_path):
    exe, have_pb, result = _compile(tmp_path)
    if result is None:
        pytest.skip('no C++ compiler available')
    assert result.returncode == 0, result.stdout + result.stderr
    stdin = ''.join(f"{int(c['conversational'])}\t{c['text']}\n" for c in CASES)
    run = subprocess.run([str(exe)], input=stdin, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout
    assert f'all {len(CASES)} who cases passed' in run.stdout
    if not have_pb:
        pytest.skip('Playerbots who worktree not present; chatter header verified only')
