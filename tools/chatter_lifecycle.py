"""Chatter 2.0 conversation lifecycle registry, shared by every surface.

A *scope* is one audience in which bots talk: a General zone, a party, a
guild or a /say area. Per scope the registry tracks, in memory and bounded:

- an active conversation window, so a second ambient conversation never
  starts on top of one that is still being delivered;
- player precedence: after a real player speaks in the scope, ambient and
  bot-to-bot initiation stays quiet for a while so the player's turn and its
  reply own the moment;
- a minimum gap between bot-initiated exchanges;
- a reply-chain depth, so bot-to-bot replies (proximity_reply,
  follow-ups) end naturally instead of looping;
- fingerprints of recent lines, so a scope does not repeat itself.

Everything is O(1) per call under one lock and never touches a database,
the network or the world thread. State is advisory and resets on restart.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import re
import threading
import time

_WORD = re.compile(r'[a-z0-9]+')
_STOP = frozenset('the a an and or to of in on is it i you we they at for with this that be '
                  'are was so just my your our me im its'.split())


def fingerprint(text):
    return frozenset(w for w in _WORD.findall((text or '').casefold()) if w not in _STOP and len(w) > 2)


def similar(a, b, threshold=0.6):
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= threshold


@dataclass
class ScopeState:
    busy_until: float = 0.0
    player_until: float = 0.0
    next_initiation: float = 0.0
    chain: int = 0
    chain_until: float = 0.0
    recent: deque = field(default_factory=lambda: deque(maxlen=8))


class Lifecycle:
    def __init__(self, *, player_quiet=45.0, initiation_gap=20.0, conversation_seconds=40.0,
                 max_chain=3, chain_window=90.0, max_scopes=4096, clock=time.monotonic):
        self.player_quiet = float(player_quiet)
        self.initiation_gap = float(initiation_gap)
        self.conversation_seconds = float(conversation_seconds)
        self.max_chain = int(max_chain)
        self.chain_window = float(chain_window)
        self.max_scopes = int(max_scopes)
        self.clock = clock
        self.lock = threading.Lock()
        self.scopes = OrderedDict()
        self.counters = {}

    def _state(self, scope):
        state = self.scopes.get(scope)
        if state is None:
            state = self.scopes[scope] = ScopeState()
            while len(self.scopes) > self.max_scopes:
                self.scopes.popitem(last=False)
        else:
            self.scopes.move_to_end(scope)
        return state

    def _count(self, reason):
        self.counters[reason] = self.counters.get(reason, 0) + 1

    def note_player(self, scope):
        """A real player spoke: they take precedence over bot initiation."""
        if not scope:
            return
        with self.lock:
            state = self._state(scope)
            now = self.clock()
            state.player_until = now + self.player_quiet
            state.chain = 0  # a human line resets any bot reply chain

    def admit_initiation(self, scope, *, conversation=False, talkativeness=50):
        """Whether bots may start talking in ``scope`` now. Returns (ok, reason).

        Talkative speakers wait less between initiations, quiet ones more
        (0.5x..1.5x of the configured gap); personality never bypasses player
        precedence or an active conversation.
        """
        if not scope:
            return True, 'unscoped'
        with self.lock:
            state = self._state(scope)
            now = self.clock()
            if now < state.player_until:
                reason = 'player_precedence'
            elif now < state.busy_until:
                reason = 'conversation_active'
            elif now < state.next_initiation:
                reason = 'initiation_gap'
            else:
                factor = 1.5 - max(0, min(100, int(talkativeness))) / 100.0
                state.next_initiation = now + self.initiation_gap * factor
                if conversation:
                    state.busy_until = now + self.conversation_seconds
                self._count('admitted')
                return True, 'admitted'
            self._count(reason)
            return False, reason

    def admit_reply(self, scope):
        """Bot-to-bot reply chains end after ``max_chain`` turns per window."""
        if not scope:
            return True, 'unscoped'
        with self.lock:
            state = self._state(scope)
            now = self.clock()
            if now >= state.chain_until:
                state.chain = 0
            if now < state.player_until and state.chain == 0:
                # A player is mid-conversation here; do not start a bot thread.
                self._count('player_precedence')
                return False, 'player_precedence'
            if state.chain >= self.max_chain:
                self._count('chain_limit')
                return False, 'chain_limit'
            state.chain += 1
            state.chain_until = now + self.chain_window
            return True, 'admitted'

    def end_conversation(self, scope):
        with self.lock:
            if scope in self.scopes:
                self.scopes[scope].busy_until = 0.0

    def is_repeat(self, scope, text):
        """Near-duplicate of a recent line in the same scope (and remember it)."""
        if not scope:
            return False
        print_ = fingerprint(text)
        with self.lock:
            state = self._state(scope)
            if len(print_) >= 3 and any(similar(print_, old) for old in state.recent):
                self._count('repeat')
                return True
            state.recent.append(print_)
            return False

    def snapshot(self):
        with self.lock:
            return {'scopes': len(self.scopes), **self.counters}


_registry = Lifecycle()


def configure(config):
    global _registry
    get = config.get
    _registry = Lifecycle(
        player_quiet=float(get('LLMChatter.Lifecycle.PlayerQuietSeconds', 45)),
        initiation_gap=float(get('LLMChatter.Lifecycle.InitiationGapSeconds', 20)),
        conversation_seconds=float(get('LLMChatter.Lifecycle.ConversationSeconds', 40)),
        max_chain=int(get('LLMChatter.Lifecycle.MaxReplyChain', 3)),
        max_scopes=int(get('LLMChatter.Lifecycle.MaxScopes', 4096)),
    )
    return _registry


def registry():
    return _registry
