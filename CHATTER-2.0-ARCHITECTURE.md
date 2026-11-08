# Chatter 2.0: Architecture

Branch `codex/chatter-2.0`, based on `40a107bbe19ae841de3294e9fb83a27fbcf5483a` (`codex/release1-candidate`, Patch 1).
Normal mode only. RP mode is untouched and not required.

## 1. Starting point (verified 2026-11-08)

| Revision | Content | Status |
|---|---|---|
| `53c6ce1` | `coop-upstream` foundation | Historical. The Nov 3 inventory recorded the bridge on this. |
| `4fda7a8` | Phase 1: GUID personality profiles | Committed |
| `4250881` | Phase 2: relationships and memory | Committed |
| `2ec707e` | Async batch delivery (`codex/perf-delivery`) | Committed |
| `15b7e46` | Phase 3: RSS | Committed |
| `43790e4` | Integration of the above | **Deployed** as Release 1.1 (per the Nov 8 audit's release manifest; not rechecked by me) |
| `40a107b` | Patch 1: audience binding, one Party responder, relay retired | Local only, **not deployed** |

Before this work, every Chatter worktree was clean. The prior audits were read. The Guide audit existed only inside a Codex session transcript, and its text was extracted from there. `mod-llm-guide@5fe0819` was cloned read-only into a scratch directory.

## 2. Message lifecycle before 2.0 (Patch 1)

```
player chat ─► C++ hook (filters: bots, addon, prefixes, Playerbots commands)
            ─► reactive_audience + reactive_turn captured ─► llm_chatter_events
bridge ─► CAS claim ─► handler ─► select_party_responder (regex gate)
       ─► reserve_party_reply (GET_LOCK + llm_reactive_reply_turn)
       ─► large per-surface prompt (traits, tone, talents, zone lore, memories)
       ─► call_llm ─► llm_chatter_messages
C++ delivery (async batch) ─► audience revalidation + expiry ─► send ─► scoped transcript
```

These gaps remained before 2.0:

- No live state in reactive replies.
- No verified gameplay facts beyond link names and item stats.
- No whisper surface.
- No way to drop a reply whose premise changed while it was being generated.
- Responder choice limited to address, then role regex, then least recent.
- Model outages produced silence only, and no circuit breaker existed.

## 3. Target architecture

```
           ┌─────────────── worldserver (C++, world thread) ───────────────┐
 ingress   │ Party hook ─┬─► BuildReactiveLiveJson (eligible bots only;    │
           │ Whisper hook┘    rare grid scan only if turn mentions rares)  │
           │ General hook (unchanged)                                       │
           └──────────────────────────────┬────────────────────────────────┘
                                          ▼ llm_chatter_events (+live)
 bridge   decide (chatter_social) ─► reserve (chatter_reactive / chatter_whisper)
          ─► context (chatter_live: selected bot only)
          ─► knowledge (chatter_knowledge: deterministic, read-only, cached)
          ─► evidence block (chatter_evidence: typed, fresh, audience-safe, budgeted)
          ─► prompt (chatter_dialogue: one contract) + relationship enrichment (Phase 2)
          ─► call_llm (deadline + circuit breaker) or deterministic fallback
          ─► claims derived from rendered evidence ─► llm_chatter_messages.claim_json
                                          ▼
 delivery  audience + expiry (Patch 1) ─► ReactiveClaimsHold (live re-read) ─► send
```

### Components and ownership

| Component | File | Owns |
|---|---|---|
| Evidence model | `tools/chatter_evidence.py` | Source, status, audience, observer, world copy, capture time, TTL, priority budget |
| Live context | `src/LLMChatterLiveContext.*`, `tools/chatter_live.py` | Party-visible own state; visible rares; parsing into evidence |
| Claim policy | `src/LLMChatterClaimPolicy.h` | Wire format and send-time decisions (header-only, unit tested) |
| Knowledge engine | `tools/chatter_knowledge.py` | Intent routing, pooled read-only world queries, revision-keyed cache |
| Social layer | `tools/chatter_social.py` | Silence, addressing, supersession, duplicate questions, single-responder ranking |
| Dialogue contract | `tools/chatter_dialogue.py` | Prompt, voice line, reply parsing, claim derivation, fallback lines |
| Reply pipeline | `tools/chatter_reply.py` | Orchestration and deadlines for Party and whispers; public knowledge for General |
| Whispers | `src/LLMChatterWhisper.*`, `tools/chatter_whisper.py` | Ingress filtering, pair binding, reservation, handler |
| Tracing | `tools/chatter_trace.py` | Content-free decision records and counters |
| Provider resilience | `tools/chatter_llm.py` | Circuit breaker, deadline skip (timeouts and retries already existed) |

### Evidence types and boundaries

| Source | Examples | May be heard by | Lifetime |
|---|---|---|---|
| `observed` | Own mana, alive, combat; a rare it can see | The audience the speaker is answering (party or the whisper pair) | 6–8 s for prompts, revalidated at send |
| `server_data` | Quest starter, objectives, loot table membership, configured NPCs | Public | Until world data changes (revision key) |
| `character` | Quest log, gear | Owner pair only (`private`) | Not used in 2.0 replies yet |
| `group` | Scoped transcript | Captured audience (Patch 1) | Session |
| `memory` | Relationship notes | Phase 2 audience rules | Phase 2 retention |
| `feed` | RSS headlines | Ambient only (Phase 3 label allowlist) | Phase 3 freshness |

Statuses are `found`, `partial`, `ambiguous`, `not_found`, `unsupported` and `unavailable`. The prompt maps each status to how certain the speaker may sound. `partial` and `not_found` never permit concluding absence.

These distinctions are encoded explicitly, as the brief requires:

- **Configured starter vs. can accept now.** Eligibility is always reported as "not checked". Can-I-take questions return `unsupported`.
- **Configured spawn vs. alive now.** Rares come only from a live `CanSeeOrDetect` observation. "Not seen" carries the note "does not mean it is dead or absent".
- **Loot membership vs. rate.** Chance is shown only for ungrouped, unreferenced rows. References are expanded two levels. Conditions, truncation and boss-table absence all yield `partial`.

### Audience boundaries (invariants)

1. The Patch 1 gates are preserved:
   - no General→Party relay
   - one reserved Party responder per turn
   - captured-audience binding
   - delivery revalidation and expiry
   - no wrong-channel fallbacks, extended to whispers: an unknown or whisper row never reaches the General catch-all
2. Evidence is filtered by audience before rendering. General receives only `server_data` for linked quests and items, never live or position-based facts.
3. Whispers are bound to the exact `(epoch, sender, bot)` pair. They are revalidated at send, together with the sender's ignore list. The Phase 2 relationship audience for a whisper is exactly `{sender, bot}`.
4. Playerbot commands are filtered before any history write, cooldown, event, lookup or model call, at three layers: C++ ingress, the handler, and the social layer. That includes every `@` selector and the Playerbots `who` command.

### Concurrency, caching, failure

- **World thread.** Ingress adds getters for at most 8 bots. It runs at most one bounded grid search, and only when the turn mentions rares. No DB access is added to the world thread. Delivery adds in-memory claim checks only.
- **Bridge.** Knowledge uses a 2-connection read-only pool with `MAX_EXECUTION_TIME` (300 ms), a 2 s connect timeout and the turn's own deadline. The LRU (2048 entries, 900 s) is keyed by world revision. Lookups run only after a turn is admitted, and only for recognised intents.
- **Model.** Calls are skipped when less than 1.5 s remains before expiry. The circuit breaker opens after 5 consecutive failures for 30 s. While it is open, a direct question gets an evidence-backed line or silence.
- **Database outages.** Lookups become `unavailable`. A missing `claim_json` column drops claim-bearing replies (fail closed). Every other path is unchanged.

## 4. Major design decisions

1. **Deterministic intent routing, no planning call.** Guide's tool loop adds latency, cost and disclosure risk for a one-line social reply. Native links supply exact IDs, so plain-text titles are never resolved.
2. **One model call per admitted turn.** Unchanged from Patch 1. The social layer and the knowledge engine add no model calls.
3. **Claims are derived from evidence, not from parsing replies.** Fragile text matching is avoided. Live self-state is included only on readiness-shaped turns, so unrelated replies never inherit a claim.
4. **Only mana is used as readiness power.** Rage, energy and runic power are near zero out of combat by design.
5. **Personality is reused, not regenerated.** The Phase 1 profile store is unchanged. `voice_line()` derives habits, including reactions to failure and success, at read time. No profile, relationship or memory row is migrated or rewritten.
6. **Legacy pipeline retained behind one switch** (`LLMChatter.Dialogue.Pipeline=1`) for rollback without a rebuild.
7. **Whispers are off by default** (`LLMChatter.Whisper.Enable=0`) for a staged rollout.

## 5. Compatibility

- **Schema.** One additive, idempotent migration: a nullable column plus one ENUM value. Older binaries ignore both. The new binary **requires** the migration: its delivery fetch selects `claim_json`.
- **Core and Playerbots.** No changes to core or Playerbots. Uses existing hooks (`PLAYERHOOK_CAN_PLAYER_USE_PRIVATE_CHAT`) and APIs (`Player::Whisper`, `CanSeeOrDetect`, `PlayerSocial::HasIgnore`).
- **Build.** The staging build includes all 18 installed modules, so symbol collisions with other custom modules would have surfaced. None did.
- **Upstream Chatter.** Not merged. The Nov 8 audit's per-commit decisions still apply.

## 6. Full-system integration (release candidate, `codex/chatter-2.0-full`)

The reactive architecture above now covers every dialogue surface through one shared engine. The design is documented in `CHATTER-2.0-FULL-INTEGRATION.md`; in summary:

- **Single choke points.** Every model request passes `call_llm`, so the surface engine (`chatter_surfaces.apply`) sits there, before relationship enrichment. Every message passes `insert_chat_message`, so repeat suppression sits there.
  - Events are gated in `process_single_event`: player precedence, initiation pacing and bot reply-chain caps.
  - Ambient queue rows and party idle ticks are gated by the `initiation_gate` decorator.
- **Surface registry.** About 29 explicit policies, each with channel, evidence audience, knowledge mode, RSS mode, lifecycle class, dedup and a speaker cap. Every call_llm label and registered event type maps to one, and tests fail if a new label or event is unmapped. System labels (memory summaries, tone, backstory, addressed-bot detection, vision) are explicit exceptions.
- **Lifecycle registry.** Bounded, in memory, advisory and reset on restart, keyed by audience scope. It enforces that players outrank bots, that only one conversation runs per scope, and that bot-to-bot threads end (`MaxReplyChain`).
- **Audience invariants extended.** Public surfaces (General, /say, guild) receive only `public` evidence, whispers only `private`, and party and raid also `party`. The engine never renders memories.
- **Rollback.** `Dialogue.Pipeline=1` or `Surfaces.Enable=0` makes the engine inert: identical prompts, no gating and no voice suffix.
- **Playerbots `who`.** A narrow two-sided fix: Playerbots `WhoAction` declines sentence-form parameters, and Chatter treats exactly those as conversation. One shared case table covers all three implementations.
