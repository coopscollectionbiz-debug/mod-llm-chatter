"""Offline regression tests for delivery throughput and world-thread work.

C++ behaviour is covered two ways: the pure batch policy header is
compiled and executed standalone when a compiler is available, and the
delivery/ambient/world translation units are checked for the contracts
that cannot be exercised without a worldserver (no blocking queries in
the per-row path, async callback guards, shutdown hook, config wiring).
Python behaviour is exercised with mocks; no database, bridge,
worldserver or provider is contacted.
"""

import glob
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parent
SRC = ROOT / 'src'
sys.path.insert(0, str(TOOLS))
import test_normal_mode_player_chat  # noqa: F401,E402 - optional SDK stubs
import chatter_delivery_pressure as pressure  # noqa: E402
import chatter_llm  # noqa: E402
import chatter_metrics  # noqa: E402


def _read(name):
    return (SRC / name).read_text(encoding='utf-8')


def _between(text, start, end):
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))]


@pytest.fixture(autouse=True)
def _fresh_state():
    pressure.reset_cache()
    chatter_metrics.snapshot(reset=True)
    yield
    pressure.reset_cache()
    chatter_metrics.snapshot(reset=True)


# ---------------------------------------------------------------------------
# Standalone C++ batch policy
# ---------------------------------------------------------------------------

def _find_msvc():
    patterns = [
        r'C:\Program Files\Microsoft Visual Studio\*\*\VC\Auxiliary\Build\vcvars64.bat',
        r'C:\Program Files (x86)\Microsoft Visual Studio\*\*\VC\Auxiliary\Build\vcvars64.bat',
    ]
    for pattern in patterns:
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


def _build_policy_test(tmp_path):
    source = TOOLS / 'tests' / 'cpp' / 'test_delivery_batch.cpp'
    exe = tmp_path / ('delivery_batch.exe' if os.name == 'nt' else 'delivery_batch')
    override = os.environ.get('LLMCHATTER_CXX')
    gnu = override or shutil.which('g++') or shutil.which('clang++')
    if gnu:
        cmd = [gnu, '-std=c++17', '-Wall', '-Wextra', '-Werror',
               f'-I{SRC}', str(source), '-o', str(exe)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return exe, result
    vcvars = _find_msvc() if os.name == 'nt' else None
    if not vcvars:
        return None, None
    script = tmp_path / 'build.bat'
    script.write_text(
        '@echo off\r\n'
        f'call "{vcvars}" >nul\r\n'
        f'cl /nologo /EHsc /std:c++17 /W4 /WX /I"{SRC}" "{source}" '
        f'/Fe"{exe}" /Fo"{tmp_path}\\\\"\r\n',
        encoding='utf-8',
    )
    result = subprocess.run(['cmd', '/c', str(script)],
                            capture_output=True, text=True)
    return exe, result


def test_cpp_delivery_batch_policy(tmp_path):
    exe, build = _build_policy_test(tmp_path)
    if build is None:
        pytest.skip('no C++ compiler available')
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'all checks passed' in run.stdout


def test_policy_header_has_no_game_dependencies():
    header = _read('LLMChatterDeliveryBatch.h')
    includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', header)
    assert set(includes) <= {
        'cstdint', 'string', 'unordered_map', 'unordered_set', 'vector'
    }


# ---------------------------------------------------------------------------
# Delivery translation unit contracts
# ---------------------------------------------------------------------------

def test_delivery_fetch_is_bounded_ordered_and_sequence_gated():
    source = _read('LLMChatterDelivery.cpp')
    fetch = _read('LLMChatterDeliveryPolicy.h')
    assert 'm.deliver_at <= NOW(6)' in fetch
    assert 'm.deliver_at >= DATE_SUB(NOW(6), ' in fetch
    assert 'DELIVERY_EXPIRY_SECONDS = 60' in source
    assert 'BuildFetchSql(' in source
    assert 'std::to_string(limit)' in fetch
    assert 'COALESCE(e.priority, 0) DESC, ' in fetch
    assert 'm.deliver_at ASC, m.id ASC' in fetch
    for fragment in (
        "m.channel IN ('say', 'msay')",
        'p.sequence = m.sequence - 1',
        'TIMESTAMPDIFF(MICROSECOND, ',
        'p.sequence < m.sequence', 'qp.sequence < m.sequence',
        'p.event_id = m.event_id', 'qp.queue_id = m.queue_id',
    ):
        assert fragment in fetch
    for column in ('m.queue_id', 'e.target_entry', 'AS prev_bot_guid',
                   'AS prev_npc_spawn_id', 'AS has_later_line', 'AS remaining_us'):
        assert column in fetch



def test_per_row_delivery_has_no_blocking_database_calls():
    source = _read('LLMChatterDelivery.cpp')
    body = _between(source, 'static DeliveryOutcome DeliverRow(',
                    'constexpr uint32 DELIVERY_EXPIRY_SECONDS')
    assert 'CharacterDatabase' not in body
    assert 'DirectExecute' not in body
    assert '&eventType' in body  # guild bookkeeping reuses the joined type


def test_delivery_blocking_query_only_in_rollback_path():
    source = _read('LLMChatterDelivery.cpp')
    assert 'DirectExecute' not in source
    assert source.count('CharacterDatabase.Query(') == 1
    rollback = _between(source, 'if (!sLLMChatterConfig->_deliveryAsyncEnable)',
                        '_fetchInFlight = true;')
    assert 'CharacterDatabase.Query(sql)' in rollback


def test_delivery_writes_are_single_terminal_and_batched():
    source = _read('LLMChatterDelivery.cpp')
    process = _between(source, 'void ProcessFetchedRows(QueryResult result,',
                       'void SweepExpiredMessages(')
    # No claim write: the in-memory guard replaces it.
    assert 'SET delivered = 1, drop_reason = NULL' not in source
    assert 'WHERE id IN ({})' in process
    assert 'INTERVAL 5 SECOND' in process  # legacy retry schedule
    assert 'RETRY_ROW_HOLD_SECONDS' in process
    assert 'HANDLED_ROW_HOLD_SECONDS' in process
    assert 'IsCancelled(' in process


def test_directed_drop_cascade_is_guarded_in_memory():
    source = _read('LLMChatterDelivery.cpp')
    finalize = _between(source, 'void FinalizeDroppedMessage(',
                        'struct DeliveryRow')
    assert "'cancelled_after_directed_drop'" in finalize
    assert '_cancelledConversations.Cancel(' in finalize
    assert finalize.index('_cancelledConversations.Cancel(') < finalize.index(
        "'cancelled_after_directed_drop'")


def test_async_callback_is_world_thread_and_shutdown_safe():
    source = _read('LLMChatterDelivery.cpp')
    impl = source[source.index('void DeliverPendingMessagesImpl()'):]
    assert impl.index('if (IsLLMChatterAsyncShutdown())') < impl.index(
        'BuildDeliveryFetchSql(')
    # Never stack fetches.
    assert impl.index('if (_fetchInFlight)') < impl.index(
        'CharacterDatabase.AsyncQuery(sql)')
    callback = _between(impl, '.WithCallback(', 'ProcessFetchedRows(std::move(result), issuedAt);')
    assert '_fetchInFlight = false;' in callback
    assert 'IsLLMChatterAsyncShutdown()' in callback
    assert '!sLLMChatterConfig->IsEnabled()' in callback
    # Capture a monotonic timestamp by value, never a game object or this.
    assert '[issuedAt](QueryResult result)' in callback


def test_world_script_drains_callbacks_and_hooks_shutdown():
    world = _read('LLMChatterWorld.cpp')
    update = _between(world, 'void OnUpdate(uint32 /*diff*/) override',
                      'UpdatePendingGuildLoginGreetings();')
    assert 'ProcessLLMChatterAsyncCallbacks();' in update
    assert 'WORLDHOOK_ON_SHUTDOWN}' in world
    shutdown = _between(world, 'void OnShutdown() override', '}')
    assert 'SetLLMChatterAsyncShutdown();' in shutdown
    shared = _read('LLMChatterShared.cpp')
    assert 'World::IsStopped()' in _between(
        shared, 'bool IsLLMChatterAsyncShutdown()', '}')


def test_write_helper_keeps_blocking_rollback():
    shared = _read('LLMChatterShared.cpp')
    helper = _between(shared, 'void ExecuteLLMChatterWrite(', 'void SetLLMChatterAsyncShutdown()')
    assert 'CharacterDatabase.Execute(sql);' in helper
    assert 'CharacterDatabase.DirectExecute(sql);' in helper
    gate = _between(shared, 'void RecordPartyChatGateActivity(', 'ExecuteLLMChatterWrite')
    assert 'DirectExecute' not in gate


# ---------------------------------------------------------------------------
# Ambient population scan contracts
# ---------------------------------------------------------------------------

def test_ambient_trigger_scans_bots_once_and_without_blocking_count():
    ambient = _read('LLMChatterAmbient.cpp')
    trigger = ambient[ambient.index('void TryTriggerChatter(bool capitalsOnly)'):]
    trigger = trigger[:trigger.index('\n}\n') if '\n}\n' in trigger
                      else trigger.index('\r\n}\r\n')]
    assert 'CharacterDatabase' not in trigger
    assert 'GetAllBots' not in trigger
    assert trigger.count('GetBotsInZones(') == 1
    # The scan only happens after at least one zone rolled a trigger.
    assert trigger.index('if (triggeredZones.empty())') < trigger.index(
        'GetBotsInZones(')
    assert ambient.count('sRandomPlayerbotMgr.GetAllBots()') == 1
    scan = _between(ambient, 'GetBotsInZones(\n', 'static uint32 GetDominantFactionInZone') \
        if 'GetBotsInZones(\n' in ambient else _between(
            ambient, 'GetBotsInZones(\r\n', 'static uint32 GetDominantFactionInZone')
    for guard in ('PlayerLoading()', 'IsInWorld()', 'IsAlive()',
                  'GetFaction(player) != zone->second',
                  'IsGroupedWithRealPlayer(player)'):
        assert guard in scan


def test_dominant_faction_uses_snapshot_not_session_scan():
    ambient = _read('LLMChatterAmbient.cpp')
    faction = _between(ambient, 'static uint32 GetDominantFactionInZone(uint32 zoneId)',
                       'static bool _ambientPendingKnown')
    assert 'GetAllSessions' not in faction
    assert 'GetCachedRealPlayerFactionCounts(' in faction
    assert 'urand(0, 1)' in faction  # legacy tie behaviour
    shared = _read('LLMChatterShared.cpp')
    refresh = _between(shared, 'void RefreshGeneralAudienceSnapshot()',
                       'void GetCachedRealPlayerFactionCounts(')
    assert refresh.count('GetAllSessions()') == 1
    assert '_realPlayerFactionCounts.swap(' in refresh


def test_ambient_pending_gate_is_conservative():
    ambient = _read('LLMChatterAmbient.cpp')
    refresh = _between(ambient, 'static void RefreshAmbientPendingCount()',
                       'static uint32 GetAmbientPendingEstimate()')
    assert 'if (_ambientPendingInFlight)' in refresh
    assert '_ambientQueuedAtResult = _ambientQueuedAtRefresh;' in refresh
    assert 'CharacterDatabase.Query(sql)' in refresh  # rollback only
    trigger = ambient[ambient.index('void TryTriggerChatter(bool capitalsOnly)'):]
    assert '++_ambientQueuedTotal;' in trigger
    assert '>= sLLMChatterConfig->_maxPendingRequests' in trigger


# ---------------------------------------------------------------------------
# Configuration wiring
# ---------------------------------------------------------------------------

CPP_KEYS = {
    'LLMChatter.Delivery.BatchSize': '8',
    'LLMChatter.Delivery.MaxPerChannelPerTick': '4',
    'LLMChatter.Delivery.CandidateLimit': '40',
    'LLMChatter.Delivery.ExpireSweepSeconds': '5',
    'LLMChatter.Delivery.AsyncEnable': 'true',
    'LLMChatter.Delivery.DiagnosticsIntervalSeconds': '0',
    'LLMChatter.Delivery.VerboseLog': 'false',
}
DIST_DEFAULTS = {
    'LLMChatter.Delivery.BatchSize': '8',
    'LLMChatter.Delivery.MaxPerChannelPerTick': '4',
    'LLMChatter.Delivery.CandidateLimit': '40',
    'LLMChatter.Delivery.ExpireSweepSeconds': '5',
    'LLMChatter.Delivery.AsyncEnable': '1',
    'LLMChatter.Delivery.DiagnosticsIntervalSeconds': '0',
    'LLMChatter.Delivery.VerboseLog': '0',
    'LLMChatter.Delivery.GenerationBacklogLimit': '40',
    'LLMChatter.Delivery.BacklogCheckSeconds': '5',
    'LLMChatter.LLM.TimeoutSeconds': '30',
    'LLMChatter.LLM.MaxRetries': '1',
    'LLMChatter.Diagnostics.IntervalSeconds': '0',
}


def test_new_cpp_options_are_read_with_documented_defaults():
    config = _read('LLMChatterConfig.cpp')
    for key, default in CPP_KEYS.items():
        match = re.search(re.escape(f'"{key}", ') + r'([a-z0-9]+)\)', config)
        assert match, key
        assert match.group(1) == default, key


def test_new_options_are_documented_in_conf_dist():
    dist = (ROOT / 'conf' / 'mod_llm_chatter.conf.dist').read_text(encoding='utf-8')
    for key, default in DIST_DEFAULTS.items():
        assert re.search(rf'^{re.escape(key)} = {re.escape(default)}\s*$',
                         dist, re.M), key


# ---------------------------------------------------------------------------
# Bridge: optional-generation backlog guard
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, due, fail=False):
        self.due = due
        self.fail = fail
        self.executed = []

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError('db down')
        self.executed.append(sql)

    def fetchone(self):
        return (self.due,)


class _DB:
    def __init__(self, due=0, fail=False):
        self.cursor_obj = _Cursor(due, fail)

    def cursor(self, *args, **kwargs):
        return self.cursor_obj


def test_backlog_guard_disabled_never_queries():
    db = _DB(due=1000)
    assert not pressure.optional_generation_blocked(
        db, {'LLMChatter.Delivery.GenerationBacklogLimit': '0'}, now=10.0)
    assert db.cursor_obj.executed == []


def test_backlog_guard_blocks_only_at_threshold_and_caches():
    config = {'LLMChatter.Delivery.GenerationBacklogLimit': '40',
              'LLMChatter.Delivery.BacklogCheckSeconds': '5'}
    below = _DB(due=39)
    assert not pressure.optional_generation_blocked(below, config, now=100.0)
    pressure.reset_cache()
    at = _DB(due=40)
    assert pressure.optional_generation_blocked(at, config, now=100.0)
    # Cached inside the window: no second probe.
    assert pressure.optional_generation_blocked(at, config, now=104.0)
    assert len(at.cursor_obj.executed) == 1
    at.cursor_obj.due = 0
    assert not pressure.optional_generation_blocked(at, config, now=105.5)
    assert len(at.cursor_obj.executed) == 2
    assert 'delivered = 0 AND deliver_at <= NOW()' in at.cursor_obj.executed[0]


def test_backlog_probe_failure_never_blocks():
    assert not pressure.optional_generation_blocked(
        _DB(fail=True), {}, now=1.0)


def test_backlog_guard_never_calls_a_model():
    source = (TOOLS / 'chatter_delivery_pressure.py').read_text(encoding='utf-8')
    assert 'chatter_llm' not in source and 'call_llm' not in source


def _bridge_loop_source():
    bridge = (TOOLS / 'llm_chatter_bridge.py').read_text(encoding='utf-8')
    return bridge[bridge.index('def main():'):]


def test_bridge_gates_only_optional_generation():
    loop = _bridge_loop_source()
    assert 'optional_generation_blocked(db, config)' in loop
    legacy = _between(loop, '# Legacy requests (General ambient chatter)',
                      'process_pending_requests,')
    assert 'if not delivery_pressure:' in legacy
    # Pressure is evaluated before any optional submission.
    assert loop.index('optional_generation_blocked(db, config)') < loop.index(
        '# Legacy requests (General ambient chatter)')
    idle = _between(loop, '# Idle chatter -> worker pool', 'check_idle_group_chatter,')
    assert 'and not delivery_pressure' in idle
    questions = _between(loop, '# Bot questions -> worker pool', 'check_bot_questions,')
    assert 'and not delivery_pressure' in questions
    # Player replies and gameplay events keep their own dispatch.
    events = _between(loop, '# Fetch + dispatch events', '# Idle chatter -> worker pool')
    assert 'delivery_pressure' not in events


def test_bridge_stale_sweep_is_throttled():
    bridge = (TOOLS / 'llm_chatter_bridge.py').read_text(encoding='utf-8')
    body = _between(bridge, 'def process_pending_requests(', '# Get pending requests')
    assert 'STALE_SWEEP_INTERVAL_SECONDS' in body
    assert body.index('if now - _last_stale_sweep') < body.index(
        'UPDATE llm_chatter_messages')
    assert 'STALE_SWEEP_INTERVAL_SECONDS = 30' in bridge


# ---------------------------------------------------------------------------
# Provider timeouts, retries and call accounting
# ---------------------------------------------------------------------------

def test_client_options_defaults_and_overrides():
    assert chatter_llm.llm_client_options({}, 'openai') == {
        'timeout': 30.0, 'max_retries': 1}
    assert chatter_llm.llm_client_options({}, 'ollama')['timeout'] == 120.0
    assert chatter_llm.llm_client_options({
        'LLMChatter.LLM.TimeoutSeconds': '12.5',
        'LLMChatter.LLM.MaxRetries': '0'}, 'anthropic') == {
        'timeout': 12.5, 'max_retries': 0}
    clamped = chatter_llm.llm_client_options({
        'LLMChatter.LLM.TimeoutSeconds': '-3',
        'LLMChatter.LLM.MaxRetries': '99'}, 'openai')
    assert clamped == {'timeout': 30.0, 'max_retries': 5}
    bad = chatter_llm.llm_client_options({
        'LLMChatter.LLM.TimeoutSeconds': 'x',
        'LLMChatter.LLM.MaxRetries': 'y'}, 'openai')
    assert bad == {'timeout': 30.0, 'max_retries': 1}


@pytest.mark.parametrize('provider', ['openai', 'google', 'openrouter', 'ollama', 'anthropic'])
def test_main_client_receives_timeout_and_retries(provider):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_openai = Mock(OpenAI=FakeClient)
    fake_anthropic = Mock(Anthropic=FakeClient)
    with patch.dict(sys.modules, {'openai': fake_openai, 'anthropic': fake_anthropic}), \
            patch.object(chatter_llm, '_main_client', None), \
            patch.object(chatter_llm, '_main_client_provider', None):
        chatter_llm.get_llm_client({
            'LLMChatter.Provider': provider,
            'LLMChatter.LLM.TimeoutSeconds': '17',
            'LLMChatter.LLM.MaxRetries': '1',
        })
    assert captured['timeout'] == 17.0
    assert captured['max_retries'] == 1


def test_quick_analyze_client_receives_timeout_and_retries():
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch.dict(sys.modules, {'openai': Mock(OpenAI=FakeClient)}), \
            patch.object(chatter_llm, '_quick_analyze_client', None), \
            patch.object(chatter_llm, '_quick_analyze_provider', None):
        client, provider = chatter_llm._get_quick_analyze_client({
            'LLMChatter.Provider': 'anthropic',
            'LLMChatter.QuickAnalyze.Provider': 'openai',
            'LLMChatter.OpenAI.ApiKey': 'test',
        })
    assert provider == 'openai'
    assert captured['timeout'] == 30.0 and captured['max_retries'] == 1


def _anthropic_client(side_effect=None):
    response = Mock()
    response.content = [Mock(text='ok')]
    client = Mock()
    client.messages.create = Mock(return_value=response, side_effect=side_effect)
    return client


def test_call_llm_makes_one_request_and_records_it():
    client = _anthropic_client()
    with patch('chatter_relationships.enrich_prompt', side_effect=lambda p, label: p), \
            patch('chatter_request_logger.log_request'):
        assert chatter_llm.call_llm(client, 'hello', {
            'LLMChatter.Provider': 'anthropic'}, label='ambient_statement') == 'ok'
    assert client.messages.create.call_count == 1
    counters = chatter_metrics.snapshot()['counters']
    assert counters['provider.generate.calls'] == 1
    assert 'provider.generate.failures' not in counters


def test_call_llm_failure_is_counted_without_extra_requests():
    client = _anthropic_client(side_effect=RuntimeError('timeout'))
    with patch('chatter_relationships.enrich_prompt', side_effect=lambda p, label: p), \
            patch('chatter_request_logger.log_request'):
        assert chatter_llm.call_llm(client, 'hello', {
            'LLMChatter.Provider': 'anthropic'}, label='ambient_statement') is None
    assert client.messages.create.call_count == 1
    counters = chatter_metrics.snapshot()['counters']
    assert counters['provider.generate.calls'] == 1
    assert counters['provider.generate.failures'] == 1


# ---------------------------------------------------------------------------
# Diagnostics and insert-path compatibility
# ---------------------------------------------------------------------------

def test_diagnostics_disabled_by_default(caplog):
    chatter_metrics.record_generated('party')
    with caplog.at_level(logging.INFO):
        assert not chatter_metrics.maybe_report({})
    assert '[DIAG]' not in caplog.text


def test_diagnostics_report_once_per_interval(caplog):
    config = {'LLMChatter.Diagnostics.IntervalSeconds': '60'}
    chatter_metrics.record_generated('general', 2)
    chatter_metrics.record_provider_call('generate', 'openai', 120, True)
    chatter_metrics.record_skip('delivery_backlog')
    with patch.object(chatter_metrics, '_last_report', 0.0), \
            patch.object(chatter_metrics.time, 'monotonic', return_value=1000.0), \
            caplog.at_level(logging.INFO):
        assert chatter_metrics.maybe_report(config, {'delivery_pressure': False})
        assert not chatter_metrics.maybe_report(config)
    assert caplog.text.count('[DIAG] bridge stats') == 1
    assert "'generated.general': 2" in caplog.text
    assert "'skipped.delivery_backlog': 1" in caplog.text
    assert chatter_metrics.snapshot()['counters'] == {}


def test_insert_counts_generated_and_keeps_relationship_hook():
    import chatter_db

    cursor = Mock(lastrowid=77)
    db = Mock()
    db.cursor.return_value = cursor
    with patch('chatter_relationships.note_output') as note_output:
        chatter_db.insert_chat_message(db, 5, 'Bot', 'hi', channel='general')
    note_output.assert_called_once_with(77, 5, 'Bot', 'hi')
    assert chatter_metrics.snapshot()['counters']['generated.general'] == 1
    sql = cursor.execute.call_args[0][0]
    assert 'INSERT INTO llm_chatter_messages' in sql
