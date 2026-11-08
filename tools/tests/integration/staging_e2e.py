#!/usr/bin/env python3
"""End-to-end Chatter 2.0 run on an isolated staging stack with a real client.

Everything is loopback and disposable: MySQL datadir copy (3338), staging
worldserver (8097) and authserver (3725) built from the candidate branches,
the bridge from this worktree, and the deterministic fake provider (11479).
A headless 3.3.5a client logs the player character in, so a reply only
counts when the worldserver actually delivers it to that session. A drop
because nobody is logged in cannot happen here and is never a pass.

Scenarios: whisper reply; claim revalidation drop; Party reply; '@Name'
conversation; conversational "who ..." (no Playerbots whisper, Chatter
replies); bare "who" (Playerbots command preserved, no model call); then the
same whisper/Party turns again under LLMChatter.Dialogue.Pipeline = 1
(rollback rehearsal).

Usage: python staging_e2e.py --stage C:\\WHE-Staging\\Chatter2 [--skip-start]
Test credentials are generated per run and written only to
<stage>/state/test_account.json (never printed).
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import mysql.connector

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parents[1]
sys.path.insert(0, str(HERE))
import wow_headless_client as wow  # noqa: E402

PLAYER = (2004, 'Test')
# An account bot of the player's own account (AllowAccountBots=1); random-bot
# characters such as Morthcell are refused ('not allowed to control').
BOT = (20014, 'Worgentest')
DB = dict(host='127.0.0.1', port=3338, user='root', password='', autocommit=True)


def wait_port(port, timeout):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(('127.0.0.1', port)) == 0:
                return True
        time.sleep(2)
    return False


def setup_db(stage):
    db = mysql.connector.connect(**DB)
    cur = db.cursor(dictionary=True, buffered=True)
    cur.execute('SELECT @@port AS port, @@datadir AS datadir')
    ident = cur.fetchone()
    assert ident['port'] == 3338 and 'WHE-Staging' in ident['datadir'], f'not the disposable DB: {ident}'
    cur.execute('SELECT account FROM acore_characters.characters WHERE guid=%s', (PLAYER[0],))
    account = cur.fetchone()['account']
    cur.execute('SELECT username FROM acore_auth.account WHERE id=%s', (account,))
    username = cur.fetchone()['username']
    password = secrets.token_hex(6)
    salt, verifier = wow.srp_verifier(username, password, os.urandom(32))
    cur.execute('UPDATE acore_auth.account SET salt=%s, verifier=%s, locked=0, online=0 WHERE id=%s',
                (salt, verifier, account))
    cur.execute("REPLACE INTO acore_auth.account_access (id, gmlevel, RealmID, comment) "
                "VALUES (%s, 3, -1, 'staging e2e')", (account,))
    cur.execute("UPDATE acore_auth.realmlist SET address='127.0.0.1', localAddress='127.0.0.1', port=8097 "
                "WHERE id=1")
    cur.execute('UPDATE acore_characters.characters SET online=0 WHERE guid IN (%s, %s)', (PLAYER[0], BOT[0]))
    # Start ungrouped so the invite path is exercised (disposable data only).
    cur.execute('SELECT DISTINCT guid FROM acore_characters.group_member WHERE memberGuid IN (%s, %s)',
                (PLAYER[0], BOT[0]))
    for row in cur.fetchall():
        cur.execute('DELETE FROM acore_characters.group_member WHERE guid=%s', (row['guid'],))
        cur.execute('DELETE FROM acore_characters.`groups` WHERE guid=%s', (row['guid'],))
    cur.execute('SELECT COALESCE(MAX(id),0) AS m FROM acore_characters.llm_chatter_events')
    first_event = cur.fetchone()['m']
    (stage / 'state').mkdir(exist_ok=True)
    (stage / 'state' / 'test_account.json').write_text(
        json.dumps({'username': username, 'password': password, 'note': 'disposable staging only'}),
        encoding='utf-8')
    db.close()
    return username, password, first_event


class Stack:
    def __init__(self, stage, bin_dir=None, tools=None, logs='e2e'):
        self.stage = stage
        self.bin = Path(bin_dir) if bin_dir else stage / 'build' / 'bin' / 'RelWithDebInfo'
        self.tools = Path(tools) if tools else TOOLS
        self.logs = stage / 'logs' / logs
        self.logs.mkdir(parents=True, exist_ok=True)
        self.procs = {}

    def start(self, name, args, cwd):
        out = open(self.logs / f'{name}.log', 'ab')
        self.procs[name] = subprocess.Popen(args, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL)

    def bridge(self, config, timeout=240):
        """(Re)start the bridge and wait until it reports its event registry ready.

        Cold starts vary (30-60 s were observed on this workstation), so a fixed
        sleep made whisper turns expire before the bridge was polling.
        """
        self.stop('bridge')
        log = self.logs / 'bridge.log'
        offset = log.stat().st_size if log.exists() else 0
        self.start('bridge', [sys.executable, '-B', str(self.tools / 'llm_chatter_bridge.py'),
                              '--config', str(config)], str(self.tools))
        started = time.time()
        while time.time() - started < timeout:
            with open(log, 'rb') as handle:
                handle.seek(offset)
                if b'validation errors' in handle.read():
                    break
            time.sleep(1)
        else:
            raise TimeoutError('bridge did not become ready')
        self.bridge_ready_s = round(time.time() - started, 1)
        print(f'bridge ready after {self.bridge_ready_s}s', flush=True)
        time.sleep(8)  # first poll cycle and startup maintenance

    def stop(self, name):
        proc = self.procs.pop(name, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_all(self):
        for name in ('bridge', 'worldserver', 'authserver', 'provider'):
            self.stop(name)


def db_rows(first_event):
    db = mysql.connector.connect(database='acore_characters', **DB)
    cur = db.cursor(dictionary=True, buffered=True)
    cur.execute('SELECT id, event_type, status FROM llm_chatter_events WHERE id > %s ORDER BY id', (first_event,))
    events = cur.fetchall()
    cur.execute('SELECT id, event_id, bot_guid, channel, message, claim_json, delivered, drop_reason, '
                'delivery_reason FROM llm_chatter_messages WHERE event_id > %s ORDER BY id', (first_event,))
    messages = cur.fetchall()
    db.close()
    return events, messages


def from_bot(kind, text=None):
    def match(line):
        return (line['sender'] == BOT[0] and line['type'] == kind
                and (text is None or text in line['text'].casefold()))
    return match


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', default=r'C:\WHE-Staging\Chatter2')
    parser.add_argument('--skip-start', action='store_true')
    parser.add_argument('--mode', choices=('candidate', 'rollback'), default='candidate',
                        help='rollback: Patch 1 worldserver + Patch 1 bridge on the migrated 2.0 schema')
    parser.add_argument('--bot', default='', help='guid:name of an account bot (default Worgentest)')
    args = parser.parse_args()
    global BOT
    if args.bot:
        guid, name = args.bot.split(':', 1)
        BOT = (int(guid), name)
    stage = Path(args.stage)
    username, password, first_event = setup_db(stage)
    rollback = args.mode == 'rollback'
    stack = (Stack(stage, stage / 'rollback' / 'bin', stage / 'rollback' / 'src' / 'tools', 'e2e-rollback')
             if rollback else Stack(stage))
    results = {}
    client = None
    try:
        if not args.skip_start:
            stack.start('provider', [sys.executable, str(TOOLS / 'tests' / 'fake_chat_provider.py')], str(TOOLS))
            stack.start('authserver', [str(stack.bin / 'authserver.exe'), '-c', 'configs/authserver.conf'],
                        str(stack.bin))
            stack.start('worldserver', [str(stack.bin / 'worldserver.exe'), '-c', 'configs/worldserver.conf'],
                        str(stack.bin))
            assert wait_port(3725, 60), 'authserver did not listen'
            assert wait_port(8097, 900), 'worldserver did not listen'
            time.sleep(10)
        stack.bridge(stack.bin / 'configs' / 'modules' / 'mod_llm_chatter.conf')

        key, address, realm_id = wow.authenticate('127.0.0.1', 3725, username, password)
        host, port = address.split(':')
        client = wow.WorldClient(host if host in ('127.0.0.1', 'localhost') else '127.0.0.1', int(port),
                                 username, key, realm_id)
        client.login(PLAYER[1])
        results['login'] = {'pass': True, 'guid': client.guid}
        time.sleep(5)
        client.drain()

        client.say(wow.CHAT_SAY, f'.playerbots bot add {BOT[1]}')
        line, seen = client.wait_chat(lambda l: l['type'] == 'system' and BOT[1].casefold() in l['text'].casefold(), 20)
        time.sleep(10)
        client.invite(BOT[1])
        time.sleep(8)
        ok = line is not None and not any(w in line['text'].casefold() for w in ('failure', 'not allowed', 'error'))
        results['bot_online'] = {'pass': ok, 'system': [l['text'] for l in seen][-3:]}
        client.drain()

        def turn(name, kind, text, expect, timeout=45, target='', negative=None, quiet=8, gap=0):
            # Patch 1 pacing: an unaddressed Party turn is not answered by the same
            # bot within ReactiveBotGapSeconds (12 s); space turns like a player would.
            time.sleep(gap)
            client.drain()
            client.say(kind, text, target)
            got, seen = client.wait_chat(expect, timeout) if expect else (None, [])
            extra = []
            if negative:
                bad, extra = client.wait_chat(negative, quiet)
                results[name] = {'pass': (expect is None or got is not None) and bad is None,
                                 'reply': got and got['text'], 'unexpected': bad and bad['text']}
            else:
                results[name] = {'pass': got is not None, 'reply': got and got['text']}
            results[name]['heard'] = [f"{l['type']}:{l['text'][:60]}" for l in seen + extra
                                      if l['sender'] == BOT[0] or l['type'] == 'channel']

        if rollback:
            # Patch 1 has no whisper surface and an unpatched Playerbots; check the
            # Party path and native commands still work on the migrated schema.
            turn('rollback_party_reply', wow.CHAT_PARTY, 'stagehello anyone around?', from_bot('party', 'whats up'))
            turn('rollback_at_name', wow.CHAT_PARTY, f'@{BOT[1]} stagehello how goes?', from_bot('party', 'whats up'))
            turn('rollback_who_command', wow.CHAT_PARTY, 'who', from_bot('whisper', 'playing with'), timeout=15)
            turn('rollback_follow_command', wow.CHAT_PARTY, 'follow', None, negative=from_bot('party'), quiet=10)
            raise StopIteration
        turn('whisper_reply', wow.CHAT_WHISPER, 'stagehello you around?', from_bot('whisper', 'whats up'),
             target=BOT[1])
        turn('party_reply', wow.CHAT_PARTY, 'stagehello anyone around?', from_bot('party', 'whats up'))
        turn('at_name_conversation', wow.CHAT_PARTY, f'@{BOT[1]} stagehello how goes?',
             from_bot('party', 'whats up'))
        turn('who_conversational', wow.CHAT_PARTY, 'who starts this quest?', from_bot('party', 'board'),
             negative=from_bot('whisper', 'playing with'), gap=14)
        turn('who_command', wow.CHAT_PARTY, 'who', from_bot('whisper', 'playing with'), timeout=15)
        # Readiness claim: the warrior has no low-mana evidence, so "mana sec" must never be sent.
        turn('claim_revalidation', wow.CHAT_WHISPER, 'stageparty you got mana for this?', None, target=BOT[1],
             negative=from_bot('whisper', 'mana sec'), quiet=30)

        # Reactive General is not exercised here: its candidates are random bots
        # in the speaker's zone (sessions + sRandomPlayerbotMgr), never account
        # companions, and random-bot autologin is off on this disposable realm.

        # Rollback rehearsal: bridge-only legacy pipeline, same worldserver.
        stack.bridge(stage / 'state' / 'bridge_pipeline1.conf')
        turn('rollback_whisper', wow.CHAT_WHISPER, 'stagehello still there?', from_bot('whisper'), target=BOT[1])
        turn('rollback_party', wow.CHAT_PARTY, 'stagehello still here?', from_bot('party'), gap=14)
        turn('rollback_who_command', wow.CHAT_PARTY, 'who', from_bot('whisper', 'playing with'), timeout=15)
        # Back to the candidate pipeline.
        stack.bridge(stack.bin / 'configs' / 'modules' / 'mod_llm_chatter.conf')
        turn('pipeline2_restored', wow.CHAT_WHISPER, 'stagehello back again?', from_bot('whisper', 'whats up'),
             target=BOT[1])
    except StopIteration:
        pass
    except Exception as exc:  # report, then tear down
        results['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if client:
            client.close()
        time.sleep(3)
        try:
            events, messages = db_rows(first_event)
        except Exception as exc:
            events, messages = [], [{'error': str(exc)}]
        if not args.skip_start:
            stack.stop_all()
        else:
            stack.stop('bridge')
    report = {'results': results, 'events': events, 'messages': messages}
    out = stack.logs / 'e2e_result.json'
    out.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    for name, r in results.items():
        print(f"{name:24} {'PASS' if isinstance(r, dict) and r.get('pass') else 'FAIL'} {json.dumps(r)[:220]}")
    print('report:', out)


if __name__ == '__main__':
    main()
