# Chatter 2.0: Implementation

Branch `codex/chatter-2.0` from `40a107b`. 18 files modified and 20 added, about 3,600 new lines including tests. No core or Playerbots changes.

## Subsystems

### 1. Unified evidence and context (`chatter_evidence.py`, `chatter_live.py`, `LLMChatterLiveContext.*`)

- The C++ capture runs on the world thread at Party and whisper ingress. It records the anchor's map, instance, phase and difficulty. For at most 8 eligible bots it records alive, combat, health %, power type and %, map, instance, phase, x/y, role and current target entry. Only when the turn mentions rares does it run one bounded grid search (≤80 yd, max 150) and record up to 4 rares that a bot `CanSeeOrDetect` within 100 yd. No DB access is involved.
- The bridge turns the capture into `Evidence` for the **selected bot only**. Power facts are kept for mana only. Own state is included only on readiness-shaped turns.
- Rendering filters records by freshness (6 s own state, 8 s rares), audience and world copy, ranks them by priority and caps them at 900 characters. Data is JSON-quoted and marked untrusted.

### 2. Gameplay knowledge (`chatter_knowledge.py`)

See `CHATTER-2.0-KNOWLEDGE.md`. It provides deterministic intent detection, pooled read-only access, a revision-keyed cache, typed results and explicit uncertainty.

### 3. Social decisions (`chatter_social.py`)

`decide_party` runs in this order:

1. Command rejection (defence in depth).
2. Supersession: a newer human line in the same audience.
3. The Patch 1 admission gate (acknowledgments, asides to other humans, non-question chatter, direct address).
4. Duplicate-question suppression.
5. For unaddressed turns, a deterministic score: firsthand evidence (+60), role relevance (+40), interest match (+12), helpfulness and talkativeness, familiarity with the asker (≤+10), a recency penalty and a GUID tie-break.

It always returns at most one bot, preserving Patch 1's one-reply invariant. `decide_whisper` silences acknowledgments only.

### 4. Dialogue contract (`chatter_dialogue.py`, `chatter_reply.py`)

- One prompt for reactive Party and whisper replies:
  - identity and the Phase 1 profile line, which keeps Phase 2 relationship enrichment working
  - a derived voice line: typing habits, reaction to failure, reaction to success, how it answers
  - channel and audience note
  - the last 6 scoped lines and the DATA block
  - the turn
  - length guidance: brief, normal, or detail only for how/why questions
- The system prompt is the Phase 1 normal voice contract plus evidence rules.
- Output is `{"message":"..."}`; empty means silence. Replies that leak scaffolding are discarded.
- Claims (`power_low|power_ok|alive|dead|rare`) are derived from the evidence actually rendered and queued in `claim_json`.
- Deterministic fallback lines ("mana sec", "<npc> starts it", "turn it in to <npc>", "yeah <rare> is right here") are used only when the model is unavailable, the turn is direct, and evidence fully backs the line.
- General keeps its Phase 1 builder and gains only a public `server_data` DATA block (`with_public_knowledge`).

### 5. Whispers (`LLMChatterWhisper.*`, `chatter_whisper.py`)

- **C++ hook.** `PLAYERHOOK_CAN_PLAYER_USE_PRIVATE_CHAT` never consumes the whisper and ignores `LANG_ADDON`. Before any side effect it rejects:
  - bot senders
  - non-bot recipients
  - cross-faction pairs
  - leading `@ . #`
  - `IsLikelyPlayerbotControlCommand`
  - configured ignored prefixes
- Then a per-pair cooldown (4 s) is applied and the turn is bound to `whisper:<epoch>:<sender>:<bot>`. Its live state is captured and a `bot_player_whisper` event is queued with a 20 s expiry.
- **Bridge.** Normal mode only, behind `Whisper.Enable`. The handler validates the audience against the subject and bot, repeats the command check, and reserves with a per-pair `GET_LOCK` plus `llm_reactive_reply_turn`, so there is one reply per turn and none while one is pending. It uses only that pair's transcript.
- **Delivery.** A new `ReactiveRoute::Whisper` checks pair re-derivation, the ignore list, claims and the deadline, then calls `Player::Whisper`.
- **Relationships.** The audience is exactly `{sender, bot}`.

### 6. Delivery (`LLMChatterDelivery.cpp`, `LLMChatterDeliveryPolicy.h`, `LLMChatterClaimPolicy.h`)

- The fetch selects `m.claim_json`.
- `ReactiveClaimsHold` re-reads the speaker (and the rare by spawn ID) before every reactive send. A malformed claim fails closed.
- Hysteresis is ±10 percentage points for power.
- A rare claim needs it alive, visible, the same entry and ≤100 yd.
- Route classification is per-event, and a whisper row can never fall through to the General catch-all.

### 7. Resilience and observability (`chatter_llm.py`, `chatter_trace.py`)

- **Circuit breaker.** Opens after 5 consecutive provider exceptions and stays open for 30 s; the next request after that is a probe.
- **Deadline skip.** Calls are skipped when less than 1.5 s remains before the event's server-side expiry. The deadline is computed from `TIMESTAMPDIFF` on the DB, so it is clock-skew safe.
- **Trace.** One JSON line per decision when `Trace.Enable=1`, using allow-listed fields only, with no message text. Counters always feed the existing `[DIAG]` summary.

### 8. Bridge integration fix found by the staging smoke

`fetch_pending_events` gates zone-scoped events on a real player being in the event's zone. Whispers cross zones, so `bot_player_whisper` was added to the bypass list. Without this, whisper events would never have been processed. Unit tests could not detect it.

## Files

**New**

- `src/LLMChatterClaimPolicy.h`
- `src/LLMChatterLiveContext.h`, `src/LLMChatterLiveContext.cpp`
- `src/LLMChatterWhisper.h`, `src/LLMChatterWhisper.cpp`
- `data/sql/characters/updates/20261108_chatter2_live_claims_whisper.sql`
- `tools/chatter_evidence.py`, `chatter_knowledge.py`, `chatter_live.py`, `chatter_social.py`, `chatter_dialogue.py`, `chatter_reply.py`, `chatter_trace.py`, `chatter_whisper.py`
- Tests: `tools/tests/test_chatter2_{cpp_policy,knowledge,conversation,scenarios,mysql}.py`, `tools/tests/cpp/test_chatter2_policy.cpp`
- The five `CHATTER-2.0-*.md` reports

**Modified**

| File | Change |
|---|---|
| `src/LLMChatterConfig.{h,cpp}` | 5 new options |
| `src/LLMChatterShared.{h,cpp}` | Whisper priority, epoch accessor, public role wrapper |
| `src/LLMChatterGroupCombat.cpp` | Party live capture |
| `src/LLMChatterPlayer.cpp` | Whisper hook |
| `src/LLMChatterDelivery.cpp`, `src/LLMChatterDeliveryPolicy.h` | Claims, whisper route |
| `tools/chatter_db.py` | `claim_json`, schema probe, truthy return |
| `tools/chatter_group.py` | Party v2 path behind `Dialogue.Pipeline` |
| `tools/chatter_general.py` | Public knowledge |
| `tools/chatter_llm.py` | Breaker, deadline |
| `tools/chatter_relationships.py` | Whisper audience |
| `tools/chatter_event_registry.py` | `bot_player_whisper` |
| `tools/llm_chatter_bridge.py` | Configure knowledge and trace; whisper fetch bypass |
| `conf/mod_llm_chatter.conf.dist` | Chatter 2.0 section |
| Two existing tests | Fixture column added; legacy test pinned to `Pipeline=1` |

## New interfaces

- **Python**
  - `Evidence`, `render_block`
  - `detect_intents`, `KnowledgeEngine.lookup`, `WorldReader`
  - `decide_party`, `decide_whisper`
  - `build_reply_prompt`, `derive_claims`, `fallback_reply`, `voice_line`
  - `generate_reactive_reply`, `with_public_knowledge`, `event_deadline`
  - `call_llm(..., deadline=)`, `provider_available`
  - `trace.record`
  - `insert_chat_message(..., claim_json=)`
- **C++**
  - `BuildReactiveLiveJson`, `MessageMentionsRare`, `ReactiveClaimsHold`
  - `HandlePlayerWhisperToBot`, `WhisperReactiveAudience`
  - `ReactiveProcessEpochId`, `GetChatterBotRoleName`
  - `LLMChatterClaimPolicy::*`, `ReactiveRoute::Whisper`
- **Event.** `bot_player_whisper`.
- **Column.** `llm_chatter_messages.claim_json`.

## Retained functionality

- Phase 1 profiles (unchanged store, no regeneration).
- Phase 2 relationships and memory, including privacy rules and enrichment through `call_llm`.
- Phase 3 RSS, kept to ambient labels.
- Async batch delivery.
- All Patch 1 gates.
- The General Phase 1 selection and builder.
- Ambient, idle, guild, proximity, boss, raid and BG producers. These are unchanged; they do not use the new reply contract yet.
