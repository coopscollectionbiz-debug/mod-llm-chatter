#!/usr/bin/env python3
"""Chatter 2.0 full-integration bridge benchmark (no network, no database).

Simulates a realm of N bots spread over zones, parties, guilds and /say
areas and drives a mixed event stream through the real shared engine:

  admit_event -> dispatch_context -> chatter_surfaces.apply (rules,
  audience-filtered knowledge with simulated lookup latency, RSS gate)
  -> admit_line (repeat suppression)

plus ambient/idle initiation gates and call_llm itself against a provider
that fails a configurable share of requests, so the circuit breaker and
deadline skips are exercised. Reports per-event engine CPU cost
(p50/p95/p99), lifecycle decisions, model requests avoided, and state size.

Usage: python tools/tests/bench_chatter2_full.py [--bots 2000] [--seconds 60]
       [--failure-rate 0.3] [--json out.json]
"""

import argparse
import json
import random
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import chatter_knowledge  # noqa: E402
import chatter_lifecycle  # noqa: E402
import chatter_llm  # noqa: E402
import chatter_surfaces as surfaces  # noqa: E402
from chatter_evidence import Evidence  # noqa: E402
# The running bridge imports these at startup; keep import cost out of timings.
import chatter_dialogue, chatter_personality, chatter_rss, chatter_shared  # noqa: E401,E402,F401

CONFIG = {
    'LLMChatter.Dialogue.Pipeline': '2', 'LLMChatter.Surfaces.Enable': '1', 'LLMChatter.ChatterMode': 'normal',
    'LLMChatter.Provider': 'anthropic', 'LLMChatter.Model': 'bench', 'LLMChatter.LLM.CircuitFailures': '5',
    'LLMChatter.LLM.CircuitOpenSeconds': '30', 'LLMChatter.LLM.MinReplyBudgetSeconds': '1.5',
}

# Event mix per simulated second per 1000 bots (roughly the staging shape:
# combat/group events dominate, players are a small share).
MIX = {
    'bot_group_combat': 1.6, 'bot_group_kill': 0.6, 'bot_group_loot': 0.5, 'bot_group_low_health': 0.4,
    'bot_group_quest_accept': 0.25, 'bot_group_quest_complete': 0.2, 'bot_group_dungeon_entry': 0.05,
    'bot_group_player_msg': 0.15, 'player_general_msg': 0.08, 'guild_player_message': 0.05,
    'guild_idle_chatter': 0.12, 'proximity_say': 0.35, 'proximity_conversation': 0.2,
    'proximity_reply': 0.3, 'proximity_player_say': 0.05, 'bg_score_milestone': 0.1,
    'raid_boss_pull': 0.03, 'weather_change': 0.02, 'bot_player_whisper': 0.04,
}
LABEL = {
    'bot_group_combat': 'reaction_combat', 'bot_group_kill': 'reaction_kill', 'bot_group_loot': 'reaction_loot',
    'bot_group_low_health': 'reaction_low_health', 'bot_group_quest_accept': 'reaction_quest_accept',
    'bot_group_quest_complete': 'reaction_quest_complete', 'bot_group_dungeon_entry': 'reaction_dungeon_entry',
    'bot_group_player_msg': 'group_player_msg', 'player_general_msg': 'general_player_msg',
    'guild_player_message': 'guild_player_message', 'guild_idle_chatter': 'guild_idle_chatter',
    'proximity_say': 'proximity_say', 'proximity_conversation': 'proximity_conversation',
    'proximity_reply': 'proximity_reply', 'proximity_player_say': 'proximity_player_conversation',
    'bg_score_milestone': 'reaction_bg_score', 'raid_boss_pull': 'raid_battle_cry',
    'weather_change': 'reaction_world_event', 'bot_player_whisper': 'bot_player_whisper',
}
LINES = ['the murlocs by the lake are back', 'anyone need linen cloth', 'that pull was rough',
         'heading to the inn after this', 'nice drop', 'where is the flight master', 'lol',
         'rain again in elwynn', 'who wants to run deadmines later', 'mana break after this pack']


class FakeKnowledge:
    def __init__(self, latency):
        self.latency = latency
        self.calls = 0

    def lookup(self, intents, world=None, deadline=None):
        self.calls += 1
        end = time.perf_counter() + self.latency  # simulated pooled DB round trip
        while time.perf_counter() < end:
            pass
        return [Evidence('quest', 'server_data', 'found', {'id': intents[0].entity_id, 'title': 'Bench Quest'})], {}


class FailingProvider:
    def __init__(self, rate, rng):
        self.rate = rate
        self.rng = rng
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if self.rng.random() < self.rate:
            raise RuntimeError('simulated provider failure')
        return type('R', (), {'content': [type('C', (), {'text': 'ok'})()]})()


def build_world(bots, rng):
    zones = max(8, bots // 60)
    groups = max(4, bots // 5)
    guilds = max(2, bots // 40)
    return zones, groups, guilds


def make_event(kind, world, rng, seq):
    zones, groups, guilds = world
    zone = rng.randrange(1, zones + 1)
    extra = {'zone_id': zone, 'area_id': zone * 10 + rng.randrange(4), 'group_id': rng.randrange(1, groups + 1),
             'guild_id': rng.randrange(1, guilds + 1), 'bot_guid': rng.randrange(1, 10 ** 6),
             'bot_name': f'Bot{seq % 997}', 'player_guid': rng.randrange(1, 500)}
    if 'quest' in kind:
        extra.update(quest_id=rng.randrange(1, 9000), quest_name='Bench Quest')
    if kind in surfaces.PLAYER_EVENTS:
        extra['player_message'] = rng.choice(LINES + [
            'who starts |cffffff00|Hquest:783:1|h[A Threat Within]|h|r ?'])
    return {'id': seq, 'event_type': kind, 'zone_id': zone, 'extra_data': extra}


def pct(values, q):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def run(bots, seconds, failure_rate, knowledge_ms, seed=7):
    rng = random.Random(seed)
    surfaces.configure(CONFIG)
    life = chatter_lifecycle.registry()
    life.clock = lambda: sim_now[0]
    sim_now = [0.0]
    fake_k = FakeKnowledge(knowledge_ms / 1000.0)
    chatter_knowledge._engine = fake_k
    chatter_llm._breaker = chatter_llm.CircuitBreaker(clock=lambda: sim_now[0])
    provider = FailingProvider(failure_rate, rng)
    world = build_world(bots, rng)
    rates = {k: v * bots / 1000.0 for k, v in MIX.items()}
    engine_us, admitted, gated, model_requests, model_skipped = [], 0, {}, 0, 0
    lines_dropped = 0
    seq = 0
    tracemalloc.start()
    wall = time.perf_counter()
    for second in range(seconds):
        sim_now[0] = float(second)
        # Ambient queue + party idle initiations (per zone / group cadence).
        for _ in range(max(1, bots // 200)):
            ok, reason = life.admit_initiation(f'general:{rng.randrange(1, world[0] + 1)}',
                                               conversation=rng.random() < 0.5, talkativeness=rng.randrange(101))
            gated[f'ambient.{reason}'] = gated.get(f'ambient.{reason}', 0) + 1
        for _ in range(max(1, bots // 100)):
            ok, reason = life.admit_initiation(f'party:{rng.randrange(1, world[1] + 1)}',
                                               conversation=rng.random() < 0.3, talkativeness=rng.randrange(101))
            gated[f'idle.{reason}'] = gated.get(f'idle.{reason}', 0) + 1
        for kind, rate in rates.items():
            count = int(rate) + (1 if rng.random() < rate - int(rate) else 0)
            for _ in range(count):
                seq += 1
                event = make_event(kind, world, rng, seq)
                start = time.perf_counter()
                ok, reason = surfaces.admit_event(event)
                if not ok:
                    engine_us.append((time.perf_counter() - start) * 1e6)
                    gated[f'{kind}.{reason}'] = gated.get(f'{kind}.{reason}', 0) + 1
                    continue
                admitted += 1
                with surfaces.dispatch_context(None, event):
                    surfaces.apply('You are a player. ' * 20, LABEL[kind], CONFIG, {})
                    line = rng.choice(LINES)
                    if not surfaces.admit_line(line, 0):
                        lines_dropped += 1
                engine_us.append((time.perf_counter() - start) * 1e6)
                # Model request path: breaker + deadline gate, then a fake provider.
                deadline = time.monotonic() + (0.5 if rng.random() < 0.02 else 8.0)
                before = provider.calls
                chatter_llm.call_llm(provider, 'bench prompt', CONFIG, label=LABEL[kind], deadline=deadline)
                if provider.calls > before:
                    model_requests += 1
                else:
                    model_skipped += 1
    elapsed = time.perf_counter() - wall
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    events = len(engine_us)
    return {
        'bots': bots, 'simulated_seconds': seconds, 'events': events, 'events_per_sim_second': round(events / seconds, 1),
        'admitted': admitted, 'lifecycle_quiet': sum(v for k, v in gated.items() if not k.endswith('admitted')
                                                     and not k.endswith('.unscoped')),
        'gated': dict(sorted(gated.items())),
        'repeat_openers_dropped': lines_dropped,
        'engine_us_p50': round(pct(engine_us, 0.5), 1), 'engine_us_p95': round(pct(engine_us, 0.95), 1),
        'engine_us_p99': round(pct(engine_us, 0.99), 1), 'engine_us_mean': round(statistics.mean(engine_us), 1),
        'knowledge_lookups': fake_k.calls, 'knowledge_latency_ms': knowledge_ms,
        'provider_failure_rate': failure_rate, 'model_requests_sent': model_requests,
        'model_requests_skipped_breaker_or_deadline': model_skipped,
        'lifecycle_scopes': len(life.scopes), 'peak_python_mem_mb': round(peak / 2 ** 20, 2),
        'wall_seconds': round(elapsed, 2),
        'engine_cpu_share_of_one_core': round(sum(engine_us) / 1e6 / seconds, 4),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bots', type=int, nargs='*', default=[1500, 2000, 3000, 4000])
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--failure-rate', type=float, default=0.3)
    parser.add_argument('--knowledge-ms', type=float, default=2.0)
    parser.add_argument('--json')
    args = parser.parse_args()
    import logging
    logging.disable(logging.CRITICAL)
    results = [run(b, args.seconds, args.failure_rate, args.knowledge_ms) for b in args.bots]
    for r in results:
        print(json.dumps(r))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
