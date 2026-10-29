"""Offline, zero-API quality indicators over existing request JSONL logs.

These are lexical proxies, not a claim that personality or dialogue is correct.
Compare the same labels and populations before/after; inspect samples manually.
"""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import statistics


ASSISTANT = re.compile(
    r'\b(happy to help|hope this helps|great question|feel free|let me know|'
    r'you(?:\x27|’)ve got this|you got this|how can I assist)\b', re.I)
AGREEMENT = re.compile(r'^\s*(yeah|yep|same|agreed|exactly|absolutely|true|totally)\b', re.I)
ABBREVIATION = re.compile(r'\b(idk|tbh|imo|gg|lol|lfg|np|ty|brb|afk)\b', re.I)
PROFILE = re.compile(r'PLAYER PROFILE ([^\n]+?) \[guid=(\d+),v=(\d+)\]: ([^\n]+)')


def response_lines(response):
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except (ValueError, TypeError):
            return []  # internal classifiers and malformed provider output aren't dialogue
    if isinstance(response, dict):
        response = [response]
    if not isinstance(response, list):
        return []
    return [(str(line.get('speaker') or line.get('name') or ''), line['message'])
            for line in response if isinstance(line, dict) and isinstance(line.get('message'), str)
            and line['message'].strip()]


def evaluate(records):
    channels = defaultdict(list)
    bots = defaultdict(list)
    openers = Counter()
    fingerprints = defaultdict(set)
    violations = Counter()
    multi_total = multi_agree = mentions = total = 0
    for row in records:
        lines = response_lines(row.get('response'))
        if not lines:
            continue
        prompt = row.get('prompt', '')
        profiles = {}
        for name, guid, version, description in PROFILE.findall(prompt):
            profiles[name] = (guid, description)
            fingerprints[(guid, version)].add(description)
        names = set(profiles) | {name for name, _ in lines if name}
        multi = len({name for name, _ in lines if name}) > 1
        for name, text in lines:
            total += 1
            channels[row.get('label', 'unknown')].append(text)
            words = re.findall(r"[\w']+", text.lower())
            openers[' '.join(words[:2])] += 1
            mentions += bool(names and any(re.search(r'\b' + re.escape(n) + r'\b', text, re.I) for n in names))
            if multi:
                multi_total += 1
                multi_agree += bool(AGREEMENT.search(text))
            if not name and len(profiles) == 1:
                name = next(iter(profiles))
            if name:
                identity, description = profiles.get(name, (name, ''))
                bots[identity].append(text)
                if 'capitalization=lowercase;' in description and any(c.isupper() for c in text):
                    violations['lowercase_style_proxy'] += 1
                if 'short fragments' in description and len(text) > 90:
                    violations['short_length_proxy'] += 1
    def rates(texts):
        return {
            'messages': len(texts),
            'question_rate': sum('?' in t for t in texts) / len(texts),
            'assistant_phrase_rate': sum(bool(ASSISTANT.search(t)) for t in texts) / len(texts),
            'mean_characters': statistics.mean(map(len, texts)),
            'uppercase_fraction': statistics.mean(
                sum(c.isupper() for c in t) / max(1, sum(c.isalpha() for c in t)) for t in texts),
            'abbreviation_rate': sum(bool(ABBREVIATION.search(t)) for t in texts) / len(texts),
        }
    return {
        'messages': total,
        'by_label': {label: rates(texts) for label, texts in channels.items()},
        'repeated_openers': [(opener, n) for opener, n in openers.most_common(20) if n > 1],
        'name_mention_rate': mentions / max(1, total),
        'multi_speaker_agreement_opening_rate': multi_agree / max(1, multi_total),
        'multi_speaker_messages': multi_total,
        'profile_definition_conflicts': sum(len(v) > 1 for v in fingerprints.values()),
        'style_deviation_proxies': dict(violations),
        'by_bot_style': {guid: rates(texts) for guid, texts in bots.items()},
        'limitations': 'Lexical proxies only. Old logs without GUID profiles cannot measure identity consistency. '
                      'Name counts lack player names absent from speaker metadata. Manual review remains necessary.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    malformed = 0
    def records():
        nonlocal malformed
        with args.log.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        yield row
                except ValueError:
                    malformed += 1
    report = evaluate(records())
    report['malformed_log_rows'] = malformed
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(text + '\n', encoding='utf-8')
    else:
        print(text)


if __name__ == '__main__':
    main()
