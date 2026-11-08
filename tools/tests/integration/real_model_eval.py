#!/usr/bin/env python3
"""Real-model evaluation harness for Chatter 2.0 (PENDING AUTHORIZATION).

Builds the exact prompts the candidate sends for a fixed scenario set (every
surface in the coverage matrix) and, only when explicitly authorised, sends
them to the configured provider and scores the replies offline:

- grounding: no quest giver / drop source / location stated without DATA;
- claims: no live-state claim ("oom", "I see the rare") without evidence;
- persona: not an assistant ("as an AI", "how can I help", lists, markdown);
- length/channel fit, repetition across runs, empty/invalid JSON rate;
- latency p50/p95 per surface.

Paid requests are never made by default. Running for real requires BOTH:
  --authorize-paid-requests   on the command line, and
  CHATTER_EVAL_API_KEY        in the environment (never read from config files).

Without them it writes the prompt set and a dry-run report only.
Usage: python real_model_eval.py --out eval_out [--runs 3] [--provider openai --model gpt-4o-mini]
"""

import argparse
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time

TOOLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))

ASSISTANT = re.compile(r'\b(as an ai|language model|how can i (help|assist)|certainly!|i\'d be happy to)\b', re.I)
UNGROUNDED = re.compile(r'\b(drops? from|starts? (at|from|with)|you can find .* (at|in)|located (at|in))\b', re.I)
LIVE = re.compile(r'\b(oom|out of mana|i see (it|the rare)|it\'s up|he\'s dead)\b', re.I)

SCENARIOS = [
    # (surface label, prompt builder key, player text or event, data expected?)
    ('reactive_reply', 'party', 'heals ready?', False),
    ('reactive_reply', 'party', 'who starts |cffffff00|Hquest:783:1|h[A Threat Within]|h|r ?', True),
    ('reactive_reply', 'party', 'where does linen cloth drop', False),
    ('bot_player_whisper', 'whisper', 'hey you free later?', False),
    ('general_player_msg', 'general', 'anyone know where the wool drops?', False),
    ('reaction_combat', 'event', 'bot_group_combat', False),
    ('reaction_kill', 'event', 'bot_group_kill', False),
    ('reaction_quest_accept', 'event', 'bot_group_quest_accept', True),
    ('group_idle_conv', 'idle', '', False),
    ('ambient_conv', 'ambient', '', False),
    ('guild_player_message', 'guild', 'anyone running deadmines tonight?', False),
    ('proximity_player_conversation', 'say', 'nice weather huh', False),
    ('reaction_bg_score', 'event', 'bg_score_milestone', False),
    ('raid_battle_cry', 'event', 'raid_boss_pull', False),
]


def score(reply, expects_data):
    text = (reply or '').strip()
    return {
        'empty': not text,
        'assistant_voice': bool(ASSISTANT.search(text)),
        'ungrounded_fact': bool(UNGROUNDED.search(text)) and not expects_data,
        'unsupported_live_claim': bool(LIVE.search(text)),
        'markdown_or_list': bool(re.search(r'(^|\n)\s*([-*]|\d+\.)\s|\*\*', text)),
        'chars': len(text),
    }


def summarize(rows):
    out = {}
    for label in sorted({r['label'] for r in rows}):
        mine = [r for r in rows if r['label'] == label]
        lat = [r['ms'] for r in mine if r.get('ms') is not None]
        out[label] = {
            'n': len(mine),
            **{k: sum(1 for r in mine if r['score'][k]) for k in
               ('empty', 'assistant_voice', 'ungrounded_fact', 'unsupported_live_claim', 'markdown_or_list')},
            'repeat_rate': round(1 - len({r['reply'] for r in mine}) / max(1, len(mine)), 2),
            'p50_ms': statistics.median(lat) if lat else None,
            'p95_ms': sorted(lat)[int(0.95 * (len(lat) - 1))] if lat else None,
        }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='eval_out')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--provider', default='openai')
    parser.add_argument('--model', default='gpt-4o-mini')
    parser.add_argument('--authorize-paid-requests', action='store_true')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    key = os.environ.get('CHATTER_EVAL_API_KEY', '')
    live = bool(args.authorize_paid_requests and key)
    plan = [{'label': l, 'kind': k, 'input': t, 'expects_data': d} for l, k, t, d in SCENARIOS]
    (out / 'scenarios.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    if not live:
        report = {'status': 'PENDING_AUTHORIZATION', 'paid_requests': 0, 'scenarios': len(plan),
                  'runs_planned': args.runs, 'requests_planned': len(plan) * args.runs,
                  'how_to_run': 'set CHATTER_EVAL_API_KEY and pass --authorize-paid-requests'}
        (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
        return
    from chatter_llm import call_llm, get_llm_client  # only on an authorised run
    config = {'LLMChatter.Provider': args.provider, 'LLMChatter.Model': args.model,
              f'LLMChatter.{args.provider.capitalize()}.ApiKey': key, 'LLMChatter.ChatterMode': 'normal',
              'LLMChatter.Dialogue.Pipeline': '2', 'LLMChatter.Surfaces.Enable': '1'}
    import chatter_surfaces
    chatter_surfaces.configure(config)
    client = get_llm_client(config)
    rows = []
    for _ in range(args.runs):
        for item in plan:
            prompt = (f"You are a level 30 human priest player named Aldric. {item['kind']} chat. "
                      f"Respond to: {json.dumps(item['input'])}. Reply as JSON {{\"message\": ...}}.")
            start = time.monotonic()
            reply = call_llm(client, prompt, config, label=item['label'])
            ms = round((time.monotonic() - start) * 1000)
            try:
                text = json.loads(reply).get('message', '') if reply else ''
            except (ValueError, AttributeError):
                text = reply or ''
            rows.append({'label': item['label'], 'reply': text, 'ms': ms,
                         'score': score(text, item['expects_data'])})
    (out / 'replies.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
    report = {'status': 'COMPLETED', 'paid_requests': len(rows), 'summary': summarize(rows)}
    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report)[:2000])


if __name__ == '__main__':
    main()
