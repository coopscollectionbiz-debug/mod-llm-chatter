"""Offline checks for the staging headless client (no network)."""

from pathlib import Path
import os
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'integration'))
import wow_headless_client as wow  # noqa: E402


def test_srp6_client_and_server_agree():
    n = int(wow.N_HEX, 16)
    salt, verifier = wow.srp_verifier('chattertest', 'pw1234', os.urandom(32))
    v = wow._int(verifier)
    b = wow._int(os.urandom(19))
    b_pub = wow._bytes((3 * v + pow(wow.G, b, n)) % n)
    a = wow._int(os.urandom(19))
    a_pub = wow._bytes(pow(wow.G, a, n))
    u = wow._int(wow.sha1(a_pub, b_pub))
    x = wow._int(wow.sha1(salt, wow.sha1(b'CHATTERTEST:PW1234')))
    client_s = pow((wow._int(b_pub) - 3 * pow(wow.G, x, n)) % n, a + u * x, n)
    server_s = pow(wow._int(a_pub) * pow(v, u, n), b, n)
    assert client_s == server_s
    assert len(wow._interleave(wow._bytes(client_s))) == 40


def test_arc4_is_a_symmetric_stream_cipher():
    key = os.urandom(20)
    data = os.urandom(300)
    assert wow.ARC4(key).process(data) != data
    enc, dec = wow.ARC4(key), wow.ARC4(key)
    # Chunked processing (1-byte then 3-byte header reads) must stay in sync.
    assert dec.process(enc.process(data[:7])) == data[:7]
    assert dec.process(enc.process(data[7:])) == data[7:]


def test_chat_packet_parse():
    import struct
    text = b'hey, whats up\x00'
    packet = (bytes((wow.CHAT_WHISPER,)) + struct.pack('<iQI', 0, 2010, 0) + struct.pack('<Q', 2004)
              + struct.pack('<I', len(text)) + text + b'\x00')
    line = wow.WorldClient._parse_chat(packet)
    assert line['type'] == 'whisper' and line['sender'] == 2010 and line['text'] == 'hey, whats up'


def test_loopback_only():
    with pytest.raises(ValueError):
        wow.authenticate('10.0.0.5', 3724, 'a', 'b')
