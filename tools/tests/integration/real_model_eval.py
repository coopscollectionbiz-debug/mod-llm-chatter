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
UNGROUNDED = re.compile(r"(drops? from|starts? (at|from|with)|you can (find|get) .* (at|in|from)|located (at|in)|"
                        r"from (killing )?[a-z -]*(mobs|humanoids|sheep|gnolls|murlocs))", re.I)
# Own-state claims: low power or any concrete health/mana number. Allowed only
# when the scenario's evidence supplies it.
LIVE = re.compile(r"(oom|out of mana|low on mana|i see (it|the rare)|it'?s up|he'?s dead|"
                  r"\d{1,3}\s*%\s*(mana|health|hp)|\b(at |on )?(half|full|low|quarter) (health|hp|mana))", re.I)
# Scenario input -> the live facts its evidence supplies (low power at 22%).
LIVE_ALLOWED = {'heals ready?': {'22'}}

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


BASE_CONFIG = {'LLMChatter.ChatterMode': 'normal', 'LLMChatter.Dialogue.Pipeline': '2',
               'LLMChatter.Surfaces.Enable': '1', 'LLMChatter.Knowledge.Enable': '0',
               'LLMChatter.Dialogue.MaxTokens': '160', 'LLMChatter.Temperature': '0.85',
               'LLMChatter.LLM.TimeoutSeconds': '20', 'LLMChatter.LLM.MaxRetries': '0'}
SPEAKER = {'name': 'Aldric', 'guid': 900001, 'race': 'Human', 'class': 'Priest', 'level': 30,
           'gender': 'male', 'role': 'healer'}
ASKER = 'Coop'

# Event descriptions in the shape the specialised builders produce (the
# shared engine then adds the surface rules exactly as in production).
EVENT_TEXT = {
    'bot_group_combat': 'Your group just pulled two Defias Pillagers. You are the healer.',
    'bot_group_kill': 'Your group just killed Edwin VanCleef, the final boss of the Deadmines.',
    'bot_group_quest_accept': 'Your group just accepted the quest "A Threat Within".',
    'bg_score_milestone': 'Warsong Gulch: your team now leads 2-1 in flag captures.',
    'raid_boss_pull': 'Your raid is pulling Onyxia now.',
}


CHAT_KINDS = {'party', 'whisper', 'general', 'guild', 'say'}
EVENT_TYPE = {'party': 'bot_group_player_msg', 'whisper': 'bot_player_whisper', 'general': 'player_general_msg',
              'guild': 'guild_player_message', 'say': 'proximity_player_conversation', 'idle': 'group_idle_conv',
              'ambient': 'ambient_conv'}


def build_prompt(item):
    """(prompt, evidence kinds) for one scenario, using the candidate's own builders."""
    from chatter_dialogue import build_reply_prompt
    from chatter_evidence import Evidence
    from chatter_personality import profile_for, profile_instruction
    profile = profile_for(SPEAKER['name'], SPEAKER['guid'])
    line = profile_instruction(SPEAKER['name'], SPEAKER['guid'])
    kind, text = item['kind'], item['input']
    if kind in ('party', 'whisper', 'general'):
        evidence = []
        if 'heals ready' in text:
            evidence.append(Evidence('live_self', 'observed', 'found',
                                     {'power': 'mana', 'power_pct': 22, 'power_low': True},
                                     audience='party', observer=SPEAKER['guid'], ttl=30))
        if '|hquest:' in text.casefold():
            evidence.append(Evidence('quest', 'server_data', 'found',
                                     {'id': 783, 'title': 'A Threat Within',
                                      'started_by': [{'name': 'Deputy Willem', 'zone': 'Elwynn Forest'}]}))
        notes = {'party': 'Only your party hears this.', 'whisper': f'Private whisper between you and {ASKER}.',
                 'general': 'Zone-wide General channel; strangers read it.'}
        prompt, used = build_reply_prompt(
            speaker=SPEAKER, profile_line=line, profile=profile, channel=kind if kind != 'general' else 'general',
            audience_note=notes[kind], asker=ASKER, message=text, evidence=evidence, mode='normal')
        return prompt, [e.kind for e in used]
    from chatter_shared import PromptParts
    situation = EVENT_TEXT.get(text) or {
        'idle': 'Your party is resting between pulls in Westfall. Start a short natural exchange.',
        'ambient': 'Say one natural line in the Elwynn Forest General channel.',
        'guild': f'{ASKER} in guild chat: {json.dumps(text)}',
        'say': f'{ASKER} standing next to you says: {json.dumps(text)}',
    }.get(kind, text)
    user = (f'{line}\nYou are {SPEAKER["name"]}, a level {SPEAKER["level"]} {SPEAKER["race"]} '
            f'{SPEAKER["class"]} player.\n{situation}')
    system = 'Write one short in-character chat line. Reply only as JSON: {"message": "..."}'
    return PromptParts(user, system), []


def score(reply, expects_data, scenario_input=''):
    text = (reply or '').strip().replace('’', "'")
    live = bool(LIVE.search(text))
    if live and scenario_input in LIVE_ALLOWED:
        numbers = set(re.findall(r'(\d{1,3})\s*%', text))
        live = not numbers <= LIVE_ALLOWED[scenario_input]  # evidence-backed numbers/wording are fine
    return {
        'empty': not text,
        'assistant_voice': bool(ASSISTANT.search(text)),
        'ungrounded_fact': bool(UNGROUNDED.search(text)) and not expects_data,
        'unsupported_live_claim': live,
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
    parser.add_argument('--max-requests', type=int, default=42, help='hard cap on paid requests')
    parser.add_argument('--only', default='', help='comma list: scenario labels or input substrings')
    parser.add_argument('--rescore', action='store_true', help='rescore an existing replies.json (no requests)')
    args = parser.parse_args()
    out = Path(args.out)
    if args.rescore:
        rows = json.loads((out / 'replies.json').read_text(encoding='utf-8'))
        expects = {s[2]: s[3] for s in SCENARIOS}
        for row in rows:
            row['score'] = score(row['reply'], expects.get(row['input'], False), row['input'])
        (out / 'replies.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        report['summary'] = summarize(rows)
        report['rescored'] = True
        (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({k: sum(v[k] for v in report['summary'].values()) for k in
                          ('empty', 'assistant_voice', 'ungrounded_fact', 'unsupported_live_claim', 'markdown_or_list')}))
        return
    out.mkdir(parents=True, exist_ok=True)
    key = os.environ.get('CHATTER_EVAL_API_KEY', '')
    live = bool(args.authorize_paid_requests and key)
    plan = [{'label': l, 'kind': k, 'input': t, 'expects_data': d} for l, k, t, d in SCENARIOS]
    if args.only:
        wanted = [w.strip().casefold() for w in args.only.split(',') if w.strip()]
        plan = [p for p in plan if any(w in p['input'].casefold() or w == p['label'] for w in wanted)]
    (out / 'scenarios.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    import chatter_surfaces
    chatter_surfaces.configure(BASE_CONFIG)
    # The exact prompts a run would send (after the shared surface engine), for review.
    previews = []
    for item in plan:
        prompt, used = build_prompt(item)
        final = chatter_surfaces.apply(prompt, item['label'], BASE_CONFIG)
        previews.append({'label': item['label'], 'input': item['input'], 'evidence': used,
                         'system': getattr(final, 'system_prompt', ''), 'user': getattr(final, 'user_prompt', str(final))})
    (out / 'prompts.json').write_text(json.dumps(previews, indent=2, ensure_ascii=False), encoding='utf-8')
    if not live:
        report = {'status': 'PENDING_AUTHORIZATION', 'paid_requests': 0, 'scenarios': len(plan),
                  'runs_planned': args.runs, 'requests_planned': len(plan) * args.runs,
                  'how_to_run': 'set CHATTER_EVAL_API_KEY and pass --authorize-paid-requests'}
        (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
        return
    from chatter_llm import call_llm, get_llm_client  # only on an authorised run
    from chatter_dialogue import parse_reply
    key_name = {'openai': 'OpenAI', 'anthropic': 'Anthropic', 'google': 'Google',
                'openrouter': 'OpenRouter'}[args.provider.lower()]
    config = {'LLMChatter.Provider': args.provider, 'LLMChatter.Model': args.model,
              f'LLMChatter.{key_name}.ApiKey': key, **BASE_CONFIG}
    import chatter_surfaces
    chatter_surfaces.configure(config)
    client = get_llm_client(config)
    # Record what the model said even when a grounding guard discards it, so
    # the report separates "model behaviour" from "what players would see".
    seen = {}
    original_check = chatter_surfaces.check_reply

    def recording_check(response, prompt, label):
        reason = original_check(response, prompt, label)
        seen.update(raw=response, reason=reason)
        return reason
    chatter_surfaces.check_reply = recording_check
    rows = []
    budget = min(args.max_requests, len(plan) * args.runs)
    for run in range(args.runs):
        for item in plan:
            if len(rows) >= budget:
                break
            prompt, _ = build_prompt(item)
            event = {'event_type': EVENT_TYPE.get(item['kind'], item['input']),
                     'extra_data': {'player_message': item['input'] if item['kind'] in CHAT_KINDS else '',
                                    'group_id': 1, 'guild_id': 1, 'zone_id': 12}}
            seen.clear()
            start = time.monotonic()
            with chatter_surfaces.dispatch_context(None, event):
                reply = call_llm(client, prompt, config, label=item['label'],
                                 max_tokens_override=int(config.get('LLMChatter.Dialogue.MaxTokens', 160)))
            ms = round((time.monotonic() - start) * 1000)
            text = parse_reply(reply, SPEAKER['name']) if reply else ''
            model_text = parse_reply(seen.get('raw'), SPEAKER['name']) if seen.get('raw') else text
            rows.append({'run': run, 'label': item['label'], 'input': item['input'], 'reply': text,
                         'model_reply': model_text, 'guard': seen.get('reason') or '',
                         'raw_ok': seen.get('raw') is not None or reply is not None, 'ms': ms,
                         'score': score(text, item['expects_data'], item['input']),
                         'model_score': score(model_text, item['expects_data'], item['input'])})
    (out / 'replies.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
    report = {'status': 'COMPLETED', 'paid_requests': len(rows), 'summary': summarize(rows)}
    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report)[:2000])


if __name__ == '__main__':
    main()
