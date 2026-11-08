# Chatter 2.0: Deployment plan (full-integration release candidate)

**Nothing here has been executed against production.** Every step requires explicit authorisation. Nothing has been pushed or merged.

## 0. What is being deployed

| Repository | Branch | Base | Content |
|---|---|---|---|
| mod-llm-chatter | `codex/chatter-2.0-full` | `d4554c7` (Chatter 2.0), which builds on `40a107b` (Patch 1) | Chatter 2.0 plus full-surface integration (`CHATTER-2.0-FULL-INTEGRATION.md`) |
| mod-playerbots | `codex/who-natural-language` | `caafea53` | Narrow `WhoAction` change: decline "who <sentence>" (two files) |

- **Patch 1 ships with this rollout.** The VPS runs `43790e4` (Release 1.1), so Patch 1 (`40a107b`) is deployed together with 2.0.
- **The Playerbots change is optional but recommended.** Without it, set `LLMChatter.PlayerChat.ConversationalWho = 0`. Otherwise "who starts this quest?" gets both the Playerbots "playing with" whisper and a Chatter reply.

## 1. Build requirements

- **Linux/GCC is validated.** Built on WSL Ubuntu 26.04 (GCC 15.2, CMake 4.2) against a copy of the Release1 `source-next` tree (the production module set), with only mod-llm-chatter and mod-playerbots replaced. Worldserver exits 0, with no LLMChatter warnings.
- **Core patch `43e2449cf` is recommended.** That patch ("Expose channel speech eligibility to scoped delivery") adds `Channel::CanSpeak`, and `source-next` does not include it.
  - The module detects the method at compile time (`LLMChatterChannelCompat.h`) and builds with or without the patch.
  - Without the patch, a muted bot's scoped General reply is refused by `Channel::Say` itself. It is not recorded as `reactive_speaker_cannot_use_channel`, and the row counts as delivered.
- **Re-configure CMake before building.** There are new headers, plus the new `.cpp` files from 2.0. Rebuild worldserver with the full production module set.
- **Rebuild Playerbots too** if the who change is included.

## 2. Configuration changes

Merge the `CHATTER 2.0` keys of `conf/mod_llm_chatter.conf.dist` into the production `mod_llm_chatter.conf`. Recommended first-rollout values:

| Key | First rollout | Notes |
|---|---|---|
| `LLMChatter.Dialogue.Pipeline` | 2 | `1` reverts every surface to legacy prompts and gating, with no rebuild (rehearsed) |
| `LLMChatter.Surfaces.Enable` | 1 | Shared engine for non-reactive surfaces. `0` keeps reactive 2.0 and leaves the others legacy |
| `LLMChatter.Lifecycle.*` | defaults (45 / 20 / 40 / 3 / 4096) | Player quiet time, initiation gap, conversation window, reply-chain cap, scope bound |
| `LLMChatter.PlayerChat.ConversationalWho` | 1 **only with** the Playerbots branch, otherwise 0 | Applies to world and bridge |
| `LLMChatter.Knowledge.Enable` | 1 | Preferably use a SELECT-only user (`Knowledge.User` / `Password`) |
| `LLMChatter.Context.LiveStateEnable` | 1 | |
| `LLMChatter.Whisper.Enable` | **0, then 1 after the supervised session** | Applies to world and bridge |
| `LLMChatter.Trace.Enable` | 1 during the first week | Content-free. Now includes `rejected` (unsupported claims) |

Keep provider, model, temperature, timeouts and retries **unchanged**. Model migration is a separate, authorised change.

## 3. Schema migration

- **What to apply.** `20261108_chatter2_live_claims_whisper.sql`, plus Patch 1's `20261108_reactive_chat_scope.sql` if it is absent. Both are additive and idempotent: they were re-applied twice more on the migrated disposable schema, exit 0.
- **When.** Apply before starting the new worldserver, because its delivery fetch selects `claim_json`.
- **Backwards compatibility is verified.** The Patch 1 worldserver and bridge ran a live client session on the migrated schema: Party reply, `@Name`, `who` and `follow` all worked.

## 4. Backups

Take these before deploying:

- **Database tables.** `acore_characters` `llm_chatter_*`, `llm_reactive_*`, `llm_bot_identities`, `llm_bot_memories`, `llm_group_*` and `llm_guild_*`.
- **The bridge SQLite sidecar** (personalities and relationships). **Never reset or regenerate it.**
- **Binaries and configs.** The current worldserver binary, the module `tools/` directory and the configs.

## 5. Compatibility checks before rollout

1. **Test suite.** Run `python -m pytest tools/tests -q` on the target host. Expected: **567 passed, 4 skipped**, or 571 passed with a disposable MySQL on 3337.
2. **Schema.** After the migration, confirm the `claim_json` column and the `bot_player_whisper` ENUM value exist.
3. **Legacy dry run.** With `Dialogue.Pipeline=1` and `Whisper.Enable=0`, behaviour must match Patch 1.
4. **Playerbots commands** (with the Playerbots branch):
   - bare `who` still returns the spec summary;
   - `who mining`, `@heal follow` and `follow` still work;
   - "who starts this quest?" gets no "playing with" whisper.
5. **Custom races.** Put a Worgen and a Goblin companion in a party; a Party question must get a reply. Before this release these bots were silently excluded, in Patch 1 and in production.

## 6. Rollout procedure

1. In a maintenance window, stop the bridge, then the worldserver.
2. Take the backups (§4), then apply the migration (§3).
3. Deploy the worldserver binary (with the Playerbots change, if chosen) and `tools/` together.
4. Merge the config (§2), with `Whisper.Enable=0` and `Trace.Enable=1`.
5. Start the worldserver, then the bridge.
   - Expect a **60–80 s bridge cold start** (measured on the dev workstation). Reactive turns made during that window expire, by design.
   - Check the logs for `Registry: … 0 validation errors` and `delivery stats` lines.
6. Run a supervised session: one real player with 2–4 companion bots, including a Worgen or Goblin. Try each of these:
   - "heals ready?", at full and at low mana
   - a linked-quest "who starts …?"
   - "how many bosses are in here?" inside a dungeon
   - `@Name hi`
   - `who`
   - `follow`
   - "ok ty"
7. Leave the realm idle for 15 minutes with players present. Check that:
   - ambient and idle chatter pauses after player lines;
   - bot-to-bot `/say` threads end within three replies;
   - no scope repeats itself.
8. After 24–48 h with no wrong-audience drops, enable whispers and repeat a whisper session that includes `@follow`.

## 7. Monitoring

- **`drop_reason`.** Watch `stale_*`, `unscoped_reactive_reply` (should stay around 0), `reactive_participant_unavailable` and `expired_before_delivery`.
- **Trace.**
  - Decision reasons: `not_addressed_to_bots`, `already_answered`, `conversation_moved_on`, `playerbot_command`, `not_admitted`.
  - Rejection rates: `rejected=unsupported_power_claim` and `unsupported_sighting_claim`.
- **`chatter_surfaces.stats()` counters.** Per surface: `player_precedence`, `conversation_active`, `initiation_gap`, `chain_limit`, `repeat_dropped`, `knowledge`, `rss`. If ambient volume drops too far, lower `Lifecycle.PlayerQuietSeconds` or `InitiationGapSeconds`.
- **`[DIAG]`.** `llm.circuit_open`, `llm.deadline`, provider latency and failures.
- **Delivery `fetch_ms_avg/max`.** A multi-second max was seen once while a bridge was restarting.

## 8. Rollback

**Behaviour only, no rebuild.** Change any of the following, then `.reload config` and restart the bridge:

- `LLMChatter.Dialogue.Pipeline=1`: legacy prompts and gating everywhere; the surface engine goes inert. **Rehearsed live.**
- `LLMChatter.Surfaces.Enable=0`: non-reactive surfaces return to legacy.
- `LLMChatter.PlayerChat.ConversationalWho=0`: "who …" is a command again.
- `Knowledge.Enable=0`, `Whisper.Enable=0`, `Context.LiveStateEnable=0`.

**Full rollback.**

1. Stop both processes.
2. Restore the previous worldserver binary, Playerbots and tools. Keep the additive schema in place: this was **rehearsed live** with the Patch 1 binary and bridge.
3. Discard pending reactive rows: `UPDATE llm_chatter_messages SET delivered=1, drop_reason='rollback' WHERE delivered=0 AND delivery_reason IN ('bot_group_player_msg','player_general_msg','bot_player_whisper')`.
4. Restart.

Personalities, relationships and memories are untouched in both cases.

## 9. Known limitations and remaining blockers

- **Real-model evaluation (gpt-4o-mini, 42 requests).** Tone, persona and format are good, and linked-quest answers with DATA are correct. Two problems remain:
  - **Drop-source guesses without DATA.** 6/6 replies named a drop source, some wrong (wool from sheep).
  - **Invented mana percentages.** 2/27 event lines stated a number the prompt never gave.
  - **Recommended before production:** extend the claim guard to numeric self-state on all surfaces, and make uncertain item questions defer instead of guessing. See TEST-RESULTS, "Real-model evaluation".
- **Reactive General end to end.** Not exercised on staging, because it needs random bots in the speaker's zone. It is covered by mocked tests and the Patch 1 smoke.
- **World-thread cost at 3–4k bots.** Not profiled on a populated server. The bridge engine itself was benchmarked to 4k bots at under 0.6% of one core.
- **Quest eligibility.** Never asserted, including Individual Progression.
- **Without core patch `43e2449cf`,** muted-bot General drops are not recorded (see §1).
