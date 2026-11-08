# Chatter 2.0: Deployment plan

**Nothing here has been executed against production.** These steps require explicit authorisation.

## 0. What is being deployed

- Branch `codex/chatter-2.0` (commit hash: see the release record at the end of the implementation commit message), based on `40a107b`.
- The VPS currently runs `43790e4` (Release 1.1). That means **Patch 1 (`40a107b`) is deployed together with Chatter 2.0**. Patch 1's own gates (relay off, one Party responder, audience binding) and its config defaults are part of this rollout.

## 1. Build requirements

- The same toolchain and core as Release 1.1 is required. Staging validated core `43e2449cf` on Windows MSVC 19.51. The VPS builds on Ubuntu with GCC/Clang. **Before rollout, build once on a Linux staging host:** the new C++ uses only standard headers and existing core APIs, but has not been compiled with GCC.
- **CMake re-configure is required**, because there are new `.cpp` files (`LLMChatterLiveContext.cpp`, `LLMChatterWhisper.cpp`). Then build worldserver.
- No core or Playerbots changes are included. Rebuild with the full module set exactly as in production.

## 2. Configuration changes

Merge the `CHATTER 2.0` section of `conf/mod_llm_chatter.conf.dist` into the production `mod_llm_chatter.conf`. Recommended first-rollout values:

| Key | First rollout | Notes |
|---|---|---|
| `LLMChatter.Dialogue.Pipeline` | 2 | `1` reverts Party to the legacy prompt with no rebuild |
| `LLMChatter.Knowledge.Enable` | 1 | Uses the existing DB host/user. Preferably create a SELECT-only user and set `Knowledge.User`/`Password`. |
| `LLMChatter.Context.LiveStateEnable` | 1 | |
| `LLMChatter.Context.RareScanRadius` | 80 | 0 disables rare observation |
| `LLMChatter.Whisper.Enable` | **0, then 1 after the staging session** | Must be enabled on both worldserver and bridge |
| `LLMChatter.Trace.Enable` | 1 during the first week | Content-free; the volume is one line per reactive decision |
| `LLMChatter.Diagnostics.IntervalSeconds` / `Delivery.DiagnosticsIntervalSeconds` | 60 | |

- Keep `LLMChatter.Provider`, `Model`, `Temperature`, `LLM.TimeoutSeconds` and `LLM.MaxRetries` **unchanged**. Model migration (for example away from `gpt-4o-mini`) is a separate, authorised change, per the Nov 8 audit.
- The world-side keys (`Context.*`, `Whisper.*`) are reload-safe (`.reload config`).

## 3. Schema migration

- Apply `data/sql/characters/updates/20261108_chatter2_live_claims_whisper.sql` to the characters DB. Apply Patch 1's `20261108_reactive_chat_scope.sql` too, if it is not already present.
- The migration is additive and idempotent: it was applied twice on MySQL 8.4 in staging.
- **The new worldserver requires the `claim_json` column.** Its delivery fetch selects it, so apply the migration **before** starting the new binary.
- It is compatible in both directions: old binaries ignore the column and the new ENUM value.

## 4. Backups

Take these before deploying:

- **`acore_characters`**, at minimum: `llm_chatter_*`, `llm_reactive_*`, `llm_bot_identities`, `llm_bot_memories`, `llm_group_*`, `llm_guild_*`.
- **The bridge's SQLite sidecar** (`LLMChatter.Personality.Path`, and `Relationships.Path` if separate). It holds personalities and relationships. **Never reset or regenerate it.** 2.0 reads profiles only.
- **The current worldserver binary, module tools directory and configs.**

## 5. Compatibility checks before rollout

1. Run the offline suite on the target host: `python -m pytest tools/tests -q`. Expected: 503 passed, 4 skipped (real-MySQL tests; 507 with a disposable DB on 3337).
2. Confirm that `SHOW COLUMNS FROM llm_chatter_messages LIKE 'claim_json'` returns a row after migration.
3. Confirm that the world DB has an `updates` table, which provides the cache revision key.
4. Do a dry run with `Dialogue.Pipeline=1` and `Whisper.Enable=0`: behaviour should match Patch 1.
5. Confirm that Playerbots still answers `@follow`, `who` and `stay` natively in Party and whisper, and that the trace shows `playerbot_command` with no model call.

## 6. Rollout procedure

1. In a maintenance window, stop the bridge, then stop the worldserver.
2. Back up (§4) and apply the migration (§3).
3. Deploy the new worldserver binary and the `tools/` directory together.
4. Merge the config (§2) with `Whisper.Enable=0` and `Trace.Enable=1`.
5. Start the worldserver, then the bridge. Check the bridge log for `Registry: … 0 validation errors` and the worldserver log for `delivery stats` lines.
6. Run a supervised session with one real player in a party with 2–4 bots. Ask:
   - "heals ready?" at full and at low mana
   - a linked-quest "anyone know who starts …?"
   - "how many bosses are in here?" inside a dungeon
   - an `@` command
   - "ok ty"
7. Expect one reply per eligible turn, silence for "ok ty", no reply to commands, and no `stale_party_audience` drops.
8. After 24–48 h with no wrong-audience drops, enable `Whisper.Enable=1` (world and bridge), `.reload config`, restart the bridge, and repeat a whisper session that includes `@follow`.

## 7. Monitoring

- **`drop_reason` distribution** in `llm_chatter_messages`. Watch these: `stale_live_claim`, `stale_rare_claim`, `stale_whisper_audience`, `unscoped_reactive_reply` (must stay ~0), `reactive_participant_unavailable`.
- **Trace reasons.** Ratios of `not_addressed_to_bots`, `already_answered`, `conversation_moved_on` and `playerbot_command`. Also `outcome=queued` vs `silent_reply`, and `fallback=true` counts.
- **`[DIAG]`.** `llm.circuit_open`, `llm.deadline` and provider failures/latency.
- **Delivery stats.** `fetch_ms_avg/max`, deferred counts, worldserver update time.
- **Knowledge.** The `lookup_failed` rate and `lookup_ms` in traces. Investigate if p95 exceeds 300 ms.

## 8. Rollback

- **Behaviour only, no rebuild.**
  - `LLMChatter.Dialogue.Pipeline=1` restores Patch 1 prompts.
  - `LLMChatter.Knowledge.Enable=0` stops knowledge lookups.
  - `LLMChatter.Whisper.Enable=0` stops whispers.
  - `LLMChatter.Context.LiveStateEnable=0` stops live capture.
  - Then `.reload config` and restart the bridge.
- **Full.** Stop both processes. Restore the previous worldserver binary and tools, and keep the additive schema in place. Discard pending reactive rows: `UPDATE llm_chatter_messages SET delivered=1, drop_reason='rollback' WHERE delivered=0 AND delivery_reason IN ('bot_group_player_msg','player_general_msg','bot_player_whisper')`. Then restart.
- **Personalities, relationships and memories** are unaffected in both cases.

## 9. Known limitations and remaining blockers

- **Not tested on a client or with a real model.** Steps 6–8 are the first real validation.
- **Not compiled with GCC.** Build on Linux staging first.
- **Quest eligibility, including Individual Progression, is never asserted.** A core-side private eligibility probe would be future work.
- **Ambient, idle, guild, proximity, boss, raid and BG producers** still use their Phase 1–3 prompts. Only player-directed Party, whisper and General knowledge use 2.0.
- **Party questions beginning with "who"** are Playerbots' `who` command and are answered natively, not by Chatter.
