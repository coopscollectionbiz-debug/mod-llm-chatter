"""Minimal headless WoW 3.3.5a (build 12340) client for staging integration tests.

It logs one real (non-bot) character into an *isolated* staging realm so that
reactive Chatter replies can be checked at the real delivery boundary: the
worldserver must actually send the bot's whisper/party/channel line to a
connected player session. It implements only what that needs:

- authserver SRP6 logon challenge/proof and realm list;
- world AUTH_CHALLENGE/AUTH_SESSION, ARC4 header crypto, CHAR_ENUM,
  PLAYER_LOGIN, ping and time-sync replies;
- CMSG_MESSAGECHAT (say/party/whisper/channel), CMSG_JOIN_CHANNEL,
  CMSG_GROUP_INVITE and SMSG_MESSAGECHAT parsing.

Loopback only: it refuses any host other than 127.0.0.1/localhost. The test
account is created in the disposable auth database by the harness.
"""

import hashlib
import hmac
import os
import queue
import socket
import struct
import threading
import time
import zlib

N_HEX = '894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7'
G = 7
SERVER_ENCRYPTION_KEY = bytes.fromhex('CC98AE04E897EACA12DDC09342915357')
SERVER_DECRYPTION_KEY = bytes.fromhex('C2B3723CC6AED9B5343C53EE2F4367CE')

CMSG_CHAR_ENUM = 0x037
SMSG_CHAR_ENUM = 0x03B
CMSG_PLAYER_LOGIN = 0x03D
CMSG_GROUP_INVITE = 0x06E
SMSG_GROUP_INVITE = 0x06F
CMSG_MESSAGECHAT = 0x095
SMSG_MESSAGECHAT = 0x096
CMSG_JOIN_CHANNEL = 0x097
SMSG_CHANNEL_NOTIFY = 0x099
CMSG_PING = 0x1DC
SMSG_AUTH_CHALLENGE = 0x1EC
CMSG_AUTH_SESSION = 0x1ED
SMSG_AUTH_RESPONSE = 0x1EE
SMSG_LOGIN_VERIFY_WORLD = 0x236
SMSG_TIME_SYNC_REQ = 0x390
CMSG_TIME_SYNC_RESP = 0x391
SMSG_PARTY_COMMAND_RESULT = 0x07F
SMSG_GROUP_LIST = 0x07D

CHAT_SAY, CHAT_PARTY, CHAT_WHISPER, CHAT_WHISPER_INFORM, CHAT_CHANNEL = 0x01, 0x02, 0x07, 0x09, 0x11
CHAT_SYSTEM = 0x00
LANG_UNIVERSAL = 0
LANG_COMMON = 7  # players must speak a racial language; Universal is rejected
CHAT_NAMES = {0x00: 'system', 0x01: 'say', 0x02: 'party', 0x03: 'raid', 0x07: 'whisper', 0x09: 'whisper_inform',
              0x11: 'channel', 0x2C: 'party_leader', 0x27: 'raid_leader'}


def _int(data):
    return int.from_bytes(data, 'little')


def _bytes(value, size=32):
    return value.to_bytes(size, 'little')


def sha1(*parts):
    h = hashlib.sha1()
    for part in parts:
        h.update(part)
    return h.digest()


def srp_verifier(username, password, salt):
    """(salt, verifier) as stored by AzerothCore account.salt / account.verifier."""
    n = int(N_HEX, 16)
    x = _int(sha1(salt, sha1(f'{username.upper()}:{password.upper()}'.encode())))
    return salt, _bytes(pow(G, x, n))


def _interleave(s_bytes):
    s = s_bytes
    while s and s[0] == 0:  # strip leading zero bytes in pairs (WoW SRP quirk)
        s = s[2:]
    even, odd = sha1(s[0::2]), sha1(s[1::2])
    out = bytearray(40)
    out[0::2], out[1::2] = even, odd
    return bytes(out)


class ARC4:
    def __init__(self, key):
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self.s, self.i, self.j = s, 0, 0
        self.process(bytes(1024))  # drop1024

    def process(self, data):
        s, i, j = self.s, self.i, self.j
        out = bytearray(len(data))
        for k, byte in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out[k] = byte ^ s[(s[i] + s[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)


def _loopback(host):
    if host not in ('127.0.0.1', 'localhost'):
        raise ValueError(f'headless client is loopback-only, refusing {host!r}')


def _recv_exact(sock, size):
    data = b''
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError('connection closed')
        data += chunk
    return data


def authenticate(host, port, username, password, timeout=10):
    """SRP6 logon. Returns (session_key K, realm address 'ip:port', realm_id)."""
    _loopback(host)
    user = username.upper().encode()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        body = (b'WoW\x00' + bytes((3, 3, 5)) + struct.pack('<H', 12340) + b'68x\x00' + b'niW\x00' + b'SUne'
                + struct.pack('<iI', 0, 0x0100007F) + bytes((len(user),)) + user)
        sock.sendall(bytes((0x00, 0x08)) + struct.pack('<H', len(body)) + body)
        head = _recv_exact(sock, 3)
        if head[2] != 0:
            raise PermissionError(f'logon challenge failed: {head[2]}')
        b_pub = _recv_exact(sock, 32)
        g_len = _recv_exact(sock, 1)[0]
        g = _int(_recv_exact(sock, g_len))
        n_len = _recv_exact(sock, 1)[0]
        n_bytes = _recv_exact(sock, n_len)
        salt = _recv_exact(sock, 32)
        _recv_exact(sock, 16 + 1)  # version challenge + security flags
        n, b = _int(n_bytes), _int(b_pub)
        a = _int(os.urandom(19))
        a_pub = _bytes(pow(g, a, n))
        u = _int(sha1(a_pub, b_pub))
        x = _int(sha1(salt, sha1(user + b':' + password.upper().encode())))
        s = pow((b - 3 * pow(g, x, n)) % n, a + u * x, n)
        k = _interleave(_bytes(s))
        hn, hg = sha1(n_bytes), sha1(_bytes(g, 1))
        m1 = sha1(bytes(i ^ j for i, j in zip(hn, hg)), sha1(user), salt, a_pub, b_pub, k)
        sock.sendall(b'\x01' + a_pub + m1 + bytes(20) + b'\x00\x00')
        proof = _recv_exact(sock, 2)
        if proof[1] != 0:
            raise PermissionError(f'logon proof failed: {proof[1]}')
        m2 = _recv_exact(sock, 20)
        _recv_exact(sock, 4 + 4 + 2)
        if m2 != sha1(a_pub, m1, k):
            raise PermissionError('server proof mismatch')
        sock.sendall(b'\x10' + bytes(4))
        size = struct.unpack('<H', _recv_exact(sock, 3)[1:])[0]
        data = _recv_exact(sock, size)
        count = struct.unpack_from('<H', data, 4)[0]
        pos, realm = 6, None
        for _ in range(count):
            _rtype, _locked, flags = data[pos], data[pos + 1], data[pos + 2]
            pos += 3
            end = data.index(b'\x00', pos)
            name = data[pos:end].decode()
            pos = end + 1
            end = data.index(b'\x00', pos)
            address = data[pos:end].decode()
            pos = end + 1 + 4 + 1 + 1
            realm_id = data[pos]
            pos += 1
            if flags & 0x04:
                pos += 5
            realm = realm or (address, realm_id, name)
    if realm is None:
        raise LookupError('no realm listed')
    return k, realm[0], realm[1]


class WorldClient:
    def __init__(self, host, port, username, session_key, realm_id=1, log=None):
        _loopback(host)
        self.username = username.upper()
        self.key = session_key
        self.realm_id = realm_id
        self.sock = socket.create_connection((host, port), timeout=15)
        self.encrypt = self.decrypt = None
        self.send_lock = threading.Lock()
        self.chat = queue.Queue()
        self.events = queue.Queue()
        self.guid = 0
        self.closed = False
        self.log = log or (lambda *_: None)
        self.ping_seq = 0
        self.language = LANG_COMMON

    # -- framing -------------------------------------------------------
    def send(self, opcode, payload=b''):
        header = struct.pack('>H', len(payload) + 4) + struct.pack('<I', opcode)
        with self.send_lock:
            if self.encrypt:
                header = self.encrypt.process(header)
            self.sock.sendall(header + payload)

    def recv(self):
        first = _recv_exact(self.sock, 1)
        if self.decrypt:
            first = self.decrypt.process(first)
        if first[0] & 0x80:
            rest = _recv_exact(self.sock, 4)
            if self.decrypt:
                rest = self.decrypt.process(rest)
            size = ((first[0] & 0x7F) << 16) | (rest[0] << 8) | rest[1]
            opcode = struct.unpack('<H', rest[2:4])[0]
        else:
            rest = _recv_exact(self.sock, 3)
            if self.decrypt:
                rest = self.decrypt.process(rest)
            size = (first[0] << 8) | rest[0]
            opcode = struct.unpack('<H', rest[1:3])[0]
        return opcode, _recv_exact(self.sock, size - 2)

    # -- session -------------------------------------------------------
    def login(self, character_name, timeout=30):
        opcode, data = self.recv()
        if opcode != SMSG_AUTH_CHALLENGE:
            raise ConnectionError(f'expected auth challenge, got {opcode:#x}')
        server_seed = data[4:8]
        client_seed = os.urandom(4)
        digest = sha1(self.username.encode(), bytes(4), client_seed, server_seed, self.key)
        addons = struct.pack('<II', 0, 0)  # addon count 0, current time 0
        addon_blob = struct.pack('<I', len(addons)) + zlib.compress(addons)
        payload = (struct.pack('<II', 12340, 0) + self.username.encode() + b'\x00'
                   + struct.pack('<I', 0) + client_seed + struct.pack('<III', 0, 0, self.realm_id)
                   + struct.pack('<Q', 3) + digest + addon_blob)
        self.send(CMSG_AUTH_SESSION, payload)
        self.encrypt = ARC4(hmac.new(SERVER_DECRYPTION_KEY, self.key, hashlib.sha1).digest())
        self.decrypt = ARC4(hmac.new(SERVER_ENCRYPTION_KEY, self.key, hashlib.sha1).digest())
        deadline = time.time() + timeout
        while time.time() < deadline:
            opcode, data = self.recv()
            if opcode == SMSG_AUTH_RESPONSE:
                if data[0] != 0x0C:
                    raise PermissionError(f'world auth failed: {data[0]:#x}')
                break
        self.send(CMSG_CHAR_ENUM)
        while time.time() < deadline:
            opcode, data = self.recv()
            if opcode == SMSG_CHAR_ENUM:
                self.guid = self._find_character(data, character_name)
                break
        if not self.guid:
            raise LookupError(f'character {character_name} not on this account')
        self.send(CMSG_PLAYER_LOGIN, struct.pack('<Q', self.guid))
        while time.time() < deadline:
            opcode, data = self.recv()
            if opcode == SMSG_LOGIN_VERIFY_WORLD:
                threading.Thread(target=self._reader, daemon=True).start()
                threading.Thread(target=self._pinger, daemon=True).start()
                return self.guid
            self._handle(opcode, data)
        raise TimeoutError('player login did not complete')

    @staticmethod
    def _find_character(data, name):
        count, pos = data[0], 1
        for _ in range(count):
            guid = struct.unpack_from('<Q', data, pos)[0]
            pos += 8
            end = data.index(b'\x00', pos)
            found = data[pos:end].decode()
            pos = end + 1 + 8 + 1 + 4 + 4 + 12 + 4 + 4 + 4 + 1 + 12 + 23 * 9
            if found.casefold() == name.casefold():
                return guid
        return 0

    def _pinger(self):
        while not self.closed:
            time.sleep(20)
            self.ping_seq += 1
            try:
                self.send(CMSG_PING, struct.pack('<II', self.ping_seq, 30))
            except OSError:
                return

    def _reader(self):
        self.sock.settimeout(None)
        while not self.closed:
            try:
                opcode, data = self.recv()
            except (OSError, ConnectionError):
                self.closed = True
                return
            try:
                self._handle(opcode, data)
            except Exception as exc:  # keep reading; one odd packet must not stop the session
                self.log('handler error', opcode, exc)

    def _handle(self, opcode, data):
        if opcode == SMSG_TIME_SYNC_REQ:
            counter = struct.unpack_from('<I', data)[0]
            self.send(CMSG_TIME_SYNC_RESP, struct.pack('<II', counter, int(time.monotonic() * 1000) & 0xFFFFFFFF))
        elif opcode == SMSG_MESSAGECHAT:
            try:
                self.chat.put(self._parse_chat(data))
            except (struct.error, ValueError, IndexError):
                pass
        elif opcode in (SMSG_GROUP_INVITE, SMSG_PARTY_COMMAND_RESULT, SMSG_GROUP_LIST, SMSG_CHANNEL_NOTIFY):
            self.events.put((opcode, data))

    @staticmethod
    def _parse_chat(data):
        kind = data[0]
        _lang, sender = struct.unpack_from('<iQ', data, 1)
        pos = 1 + 4 + 8 + 4
        channel = ''
        if kind == CHAT_CHANNEL:
            end = data.index(b'\x00', pos)
            channel = data[pos:end].decode('utf-8', 'replace')
            pos = end + 1
        receiver = struct.unpack_from('<Q', data, pos)[0]
        pos += 8
        length = struct.unpack_from('<I', data, pos)[0]
        pos += 4
        text = data[pos:pos + length].rstrip(b'\x00').decode('utf-8', 'replace')
        return {'type': CHAT_NAMES.get(kind, hex(kind)), 'sender': sender, 'receiver': receiver,
                'channel': channel, 'text': text, 'at': time.time()}

    # -- actions -------------------------------------------------------
    def say(self, kind, text, target=''):
        payload = struct.pack("<II", kind, self.language)
        if kind in (CHAT_WHISPER, CHAT_CHANNEL):
            payload += target.encode() + b'\x00'
        self.send(CMSG_MESSAGECHAT, payload + text.encode() + b'\x00')

    def join_channel(self, name, channel_id=0):
        self.send(CMSG_JOIN_CHANNEL, struct.pack('<I', channel_id) + b'\x00\x00' + name.encode() + b'\x00\x00')

    def invite(self, name):
        self.send(CMSG_GROUP_INVITE, name.encode() + b'\x00' + struct.pack('<I', 0))

    def wait_chat(self, predicate, timeout):
        """First chat line matching ``predicate`` within ``timeout`` seconds, else None."""
        end = time.time() + timeout
        seen = []
        while time.time() < end:
            try:
                line = self.chat.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                break
            seen.append(line)
            if predicate(line):
                return line, seen
        return None, seen

    def drain(self):
        lines = []
        while True:
            try:
                lines.append(self.chat.get_nowait())
            except queue.Empty:
                return lines

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
