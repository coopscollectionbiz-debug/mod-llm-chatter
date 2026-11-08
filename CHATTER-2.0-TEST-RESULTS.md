# Chatter 2.0: Test results

All results below were measured on 2026-11-08 on the development workstation. The original 2.0 sections had no WoW-client test; the **Full integration** sections at the end include real headless 3.3.5a client sessions on an isolated staging realm. No real model was called. Every model response came from a fake provider: in-process for unit tests, loopback HTTP for the staging smoke.

## Commands

From `C:\Azerothcore-dev\mod-llm-chatter-2.0`:

```powershell
$env:PYTHONUTF8 = '1'
python -m pytest tools/tests -q --disable-warnings -p no:cacheprovider -rs --basetemp=<worktree>/tools/state/pytest-final
# Real-MySQL reservation tests (disposable instance on 127.0.0.1:3337 only):
$env:CHATTER_TEST_MYSQL_PORT = '3337'
python -m pytest tools/tests/test_reactive_scope.py tools/tests/test_chatter2_mysql.py -q -p no:cacheprovider
python -m compileall -q tools
git diff --check
python C:\Azerothcore\apps\codestyle\codestyle-cpp.py
```

## Results

| Check | Baseline `40a107b` | Release candidate |
|---|---|---|
| Offline suite | 420 passed, 2 skipped, 3 subtests | **503 passed**, 4 skipped (the real-MySQL tests, run separately below), 3 subtests |
| Real-MySQL tests (disposable 8.4 on 3337) | not run | **9 passed**: Patch 1's 2 skipped tests plus 2 new whisper-reservation tests, in their files |
| Standalone C++ policy tests (MSVC `/W4 /WX`) | delivery batch | delivery batch plus **claim and route policy: all checks passed** |
| Type check of 7 changed/new TUs (real build flags, scratchpad) | — | 0 errors |
| Staging build: core `43e2449cf` plus all 18 installed modules, RelWithDebInfo | 16 min full, exit 0 | incremental 1 min after re-configure, **exit 0**, worldserver linked |
| `compileall`, `git diff --check` | — | pass |
| Codestyle on new C++ files | — | no findings (pre-existing findings in other files unchanged) |
| Migration on real MySQL 8.4 | — | applied twice; column added once; all previous ENUM values kept |

**Failed tests:** none at the end. Three existing tests needed updates for intended changes:

- one fixture table gained `claim_json`
- the Patch 1 legacy-prompt test is pinned to `Dialogue.Pipeline=1`
- the whisper hook was moved so a static ordering test still anchors on the General hook

## Coverage by requested area

| Area | Test(s) |
|---|---|
| Party / General audience isolation | `test_party_and_private_facts_never_reach_general`, `test_general_gets_only_public_server_data`, Patch 1 `test_reactive_scope` (incl. real MySQL) |
| Whisper privacy | `test_whisper_reply_is_bound_to_the_exact_pair`, `test_whisper_rejects_forged_or_mismatched_audience`, `test_whisper_relationship_audience_is_exactly_the_pair`, C++ `test_whisper_audience_is_exact_pair_and_rechecked_at_send` |
| `@` command filtering | `test_playerbot_command_selector_never_gets_a_responder`, `test_whisper_commands_and_selectors_never_reach_the_model`, C++ `test_whisper_ingress_filters_commands_before_any_side_effect`, `test_leading_who_is_left_to_the_playerbots_who_command` |
| One responder | `test_exactly_one_responder…`, `test_scenario_many_bots_one_reply`, real-MySQL concurrent reservation (Party and whisper) |
| Stale expiry / deadlines | `test_no_model_call_when_deadline_already_passed`, `test_expired_deadline_does_no_query`, `test_stale_live_snapshot_is_not_used`, real-MySQL expired-turn tests |
| Group / instance / phase changes | Claim policy (wrong power type, death, visibility loss, distance, spawn reuse); evidence world-copy filter; Patch 1 audience re-derivation |
| Bot despawning | `RareClaimNeedsLiveVisibleSameRare` (despawned), `ReactiveClaimsHold` speaker presence |
| Model timeouts / provider failures | `test_model_outage_falls_back_only…`, `test_circuit_breaker_opens…`, `test_scenario_model_outage_during_gameplay` |
| DB timeouts / missing data | `test_database_failure_becomes_unavailable…`, `test_world_reader_sets_read_only_and_statement_cap`, `test_unknown_quest_id_is_not_found…`, `has_claim_column` fail-closed |
| Queue saturation | Existing delivery backlog tests (perf branch), plus `test_scale_*` |
| Invalid links / forged labels | `test_forged_or_malformed_links_are_ignored`, `test_forged_link_label_uses_real_title` |
| Custom quest IDs | `test_custom_quest_ids_resolve…` plus a live check against the realm's quests with ID ≥ 90000 |
| Quest eligibility | `test_quest_eligibility_is_unsupported…`, `test_conditional_quests_report_conditions`, starter `eligibility: not checked` |
| Loot references | `test_reference_loot_is_expanded…`, `test_loot_reports_configured_chance_only…`, `test_boss_drop_absent…partial` |
| Rare visibility | Rare claim tests; `test_bot_that_actually_sees_the_rare_answers`; unseen-rare honesty |
| Memory privacy / relationship persistence | Phase 2 suite (unchanged, passing); `test_scenario_two_bots_remember_a_previous_dungeon_run` |
| Personality persistence | Phase 1 suite; `test_voice_line_is_stable_and_derived_without_writes` |
| RSS dedup / isolation | Phase 3 suite; `test_rss_is_never_injected_into_reactive_replies` |
| Prompt injection | `test_prompt_quotes_untrusted_input_and_evidence`, scaffolding-leak rejection, malformed-claim fail-closed |
| Duplicate suppression | `test_repeated_question_already_answered…`, reservation tests, delivery `HandledRowGuard` (existing) |

## Gameplay scenarios (mocked replays, not gameplay)

All 15 requested scenarios have a deterministic replay through production code:

| # | Scenario | Replay result |
|---|---|---|
| 1 | Healer readiness | Healer selected; "mana sec" with `power_low` claim |
| 2 | Tank pull | Tank selected; no "low rage" claim |
| 3 | Linked quest | Verified starter in DATA; eligibility marked "not checked" |
| 4 | Boss identification | Bosses scoped to the party's own map |
| 5 | Boss drop | Configured membership only; no drop rate |
| 6 | Visible rare | The observing bot answers and carries a `rare` claim; an unseen rare is "not seen", never "down" |
| 7 | Remembered run | Shared boss memory enriches the prompt |
| 8 | Disagreement | Phase 1 flexible conversations; not changed |
| 9 | Familiar player | Familiar bot preferred |
| 10 | Many bots | One reply |
| 11 | General vs Party | Separate audiences, Patch 1 |
| 12 | `@` command | Zero model calls |
| 13 | RSS | Ambient only |
| 14 | Model outage | Evidence-backed fallback; breaker stops traffic after 5 failures |
| 15 | Population | See below |

## Runtime smoke on an isolated staging stack (measured)

- **Stack.** Staging worldserver (this branch, all 18 modules) plus a disposable MySQL 8.4 copy on 127.0.0.1:3338 (migrated), the bridge from this worktree, and the loopback fake provider. No random bots, no client.
- **Method.** Synthetic whisper turns were inserted into the disposable DB.
  - **Readiness turn.** The bridge claimed it and the decision layer responded. The reply was "mana sec" with claim `power_low;g=2010;pt=0;th=35`, queued on `whisper`. The C++ delivery fetched it through the new SQL, classified it `Whisper` and dropped it as `reactive_participant_unavailable`. That is correct, since nobody was logged in.
  - **`@follow`.** Skipped as `playerbot_command`, with no provider request.
  - **Forged audience.** Skipped.
- **Delivery stats.** `fetch_ms_avg=1`, `process_us=41`, no DB or worldserver errors.
- **Two defects found and fixed by this smoke:**
  1. Whisper events were filtered out by the bridge's zone gate.
  2. The bot's own state was filtered out of whisper prompts by the audience rules.
  Both now have regression tests.

## Performance measurements (offline, this workstation)

| Measurement | Result |
|---|---|
| `decide_party`, 800 simultaneous party turns (4,000 bots) | p50 65 µs, p95 128 µs; acks, "lol" and commands never respond |
| Knowledge cache, 500 repeats of one question | 499 hits; under 12 queries total |
| Skipped turns (ack/command) | 0 lookups, 0 model calls |
| Prompt size, legacy vs 2.0, same Party turn | "heals ready?" 6,620→4,841 chars (−27%); route question 6,599→4,640 (−30%) |
| Model calls per admitted Party turn | 1 (unchanged); 0 for silent decisions |
| Real knowledge lookups (dev world DB) | 1–5 ms typical; spawn box search ~40 ms; all under 500 ms |
| Staging worldserver delivery | async fetch 1 ms average; 41 µs per processed row |

## Not run / not measurable here

- **WoW-client gameplay sessions.** There is no client automation in this environment.
- **Real-model quality.** Naturalness, false-claim rate, repetition, personality adherence and p50/p95 provider latency were not measured: no paid requests were authorised.
- **World-thread cost at 3,000–4,000 live bots.** The capture is O(eligible bots ≤ 8) plus an optional grid search, but it was not profiled on a populated server.
- **A Party end-to-end runtime smoke** (it needs group fixtures). Covered by mocked handler tests.
- **Production-model compatibility** (`gpt-4o-mini` / Luna) with the new contract. See the deployment plan.


---

# Full integration (release candidate `codex/chatter-2.0-full`)

## Commands

```powershell
cd C:\Azerothcore-dev\mod-llm-chatter-2.0-full
python -m pytest tools/tests -q                                        # offline suite
$env:CHATTER_TEST_MYSQL_PORT='3337'; python -m pytest tools/tests -q  # + disposable MySQL tests
python tools/tests/bench_chatter2_full.py --seconds 120 --failure-rate 0.3   # and 1.0
python tools/tests/integration/staging_e2e.py --stage C:\WHE-Staging\Chatter2 --bot 4006:Tester
python tools/tests/integration/staging_e2e.py --stage C:\WHE-Staging\Chatter2              # Worgen companion
python tools/tests/integration/staging_e2e.py --stage C:\WHE-Staging\Chatter2 --mode rollback --bot 4006:Tester
python tools/tests/integration/real_model_eval.py --out eval_out     # dry run: PENDING_AUTHORIZATION
# WSL (Release1-Build): C:\WHE-Staging\Chatter2\gcc_build.sh build ; gcc_policy_tests.sh
```

## Results

| Check | Result |
|---|---|
| Offline suite | **589 passed**, 4 skipped (the real-MySQL tests), 3 subtests, 0 failed (567 before the grounding guards) |
| Offline suite with the disposable MySQL on 3337 | **571 passed, 0 skipped**, 0 failed |
| New tests | `test_who_routing.py` (23; an 18-case table run against 3 implementations), `test_chatter2_surfaces.py` (24), `test_chatter2_privacy.py` (13), `test_headless_client.py` (4) |
| Standalone C++, MSVC `/W4 /WX` | claim/route policy, delivery batch, and who routing (both the chatter and the Playerbots header): all pass |
| Standalone C++, GCC 15.2 `-Wall -Wextra -Werror` (WSL) | claim/route policy, delivery batch, and **all 18 who cases (both headers)**: pass |
| Windows staging build (VS 2026, RelWithDebInfo, core `43e2449cf`, 18 modules, Playerbots pointed at the who worktree) | worldserver **exit 0** (14 min after the junction change); authserver exit 0 |
| Linux/GCC build (WSL Ubuntu 26.04, GCC 15.2, CMake 4.2, copy of `source-next`, MODULES=static) | First attempt **failed**: `Channel::CanSpeak` is missing (see below). After the fix: **exit 0** (13 min), final incremental exit 0, no LLMChatter warnings |
| `compileall`, `git diff --check` | pass |
| Codestyle on the new C++ files | No findings. Pre-existing findings in `LLMChatterGroup.cpp` are unchanged (6 before, 6 after) |
| Migration on the disposable MySQL 8.4 | Applied twice more on the migrated schema, exit 0. `claim_json` and the `bot_player_whisper` ENUM value are present |

These defects were found during this task and fixed:

1. **GCC build failure.** Patch 1 code used `Channel::CanSpeak`, which only the staging core patch provides. Fixed with `LLMChatterChannelCompat.h`.
2. **Custom races excluded.** Hard-coded race lists dropped Worgen (12) and Goblin (9) bots from Party, General, guild and BG responders. **This also affects Patch 1 and production.** The rollback rehearsal reproduced it: a Worgen companion never answered Party chat on Patch 1.
3. **Unsupported live claims.** A warrior's "mana sec" went out carrying only an `alive` claim. Fixed with `unsupported_claim`: an assertion of low power or a sighting now needs the bot's own matching evidence, otherwise the reply stays silent.
4. **`@BotName …` conversation silenced.** The 2.0 social layer had silenced these lines. Fixed.

Existing tests were updated for intended changes: the pinned race list now includes 9 and 12, and the fake provider keys off the current turn.

## End-to-end on an isolated staging realm with a real client (measured)

**Stack.** Everything is loopback and disposable:

- a MySQL datadir copy on 3338;
- a staging worldserver on 8097 and authserver on 3725, built from these branches;
- the bridge from this worktree;
- the deterministic fake provider on 11479.

**Client.** `tools/tests/integration/wow_headless_client.py` is a minimal 3.3.5a client: SRP6 logon, realm list, ARC4 header crypto, CHAR_ENUM/PLAYER_LOGIN and chat. It logged in the real character `Test` (guid 2004). The bot is added with `.playerbots bot add` and invited to the party.

**Pass rule.** A reply passes **only if the client session actually receives it** from the bot.

**Credentials.** Test credentials are generated per run into `C:\WHE-Staging\Chatter2\state\test_account.json` and never printed.

| Scenario | Human companion (`Tester`) | Worgen companion (`Worgentest`) | Patch 1 rollback (binary + bridge) on the migrated schema (`Tester`) |
|---|---|---|---|
| Login, bot online, party invite | PASS | PASS | PASS |
| Whisper → bot whisper reply delivered | PASS | PASS | n/a (Patch 1 has no whisper surface) |
| Party question → one Party reply delivered | PASS | PASS (after the race fix) | PASS |
| `@Name …` conversation | PASS | PASS | PASS |
| "who starts this quest?" → a Chatter Party reply and **no** Playerbots "playing with" whisper | PASS | PASS | not run (Patch 1 + unpatched Playerbots) |
| Bare `who` → Playerbots spec/"playing with" whisper, no model call | PASS | PASS | PASS |
| `follow` → Playerbots "Following", no Chatter line | – | – | PASS |
| "mana sec" bait with no low-mana evidence → never sent | PASS (paladin at full mana) | PASS (warrior, no mana) | n/a |
| Bridge restart with `Dialogue.Pipeline=1`: whisper, Party, `who` | PASS | PASS | n/a |
| Back to `Pipeline=2` | PASS | PASS | n/a |

**Database at the delivery boundary.** In the final runs, every reply has `delivered=1` and `drop_reason=NULL`, and appears in the client's received chat. The bait turn is a `silent_reply` trace, and nothing was queued for it.

**Not covered end to end: reactive General.** Its candidates are random bots in the speaker's zone, never account companions, and random-bot autologin is off on this disposable realm. General is covered by mocked tests (`decide_general`, `with_public_knowledge`, General pacing, faction filters) and by Patch 1's earlier smoke.

**Harness defects found and fixed along the way:**

- the client must speak Common, not Universal;
- random-bot characters cannot be controlled, so the test needs an account bot;
- the fixture must key off the current turn;
- turns must respect Patch 1's 12 s gap before the same bot replies again;
- the harness must wait for bridge readiness.

On that last point, bridge cold start measured **62–80 s** (4 s warm), so whispers sent sooner expired. This is relevant for production restarts too.

Logs from these runs are kept in `C:\WHE-Staging\Chatter2\logs\e2e*\`: `e2e_result_human.json`, `e2e_result_worgen.json` and `e2e-rollback\e2e_result.json`.

**Delivery stats during the runs:**

- `fetch_ms_avg` was 1–2 ms, with spikes of 0.7–1.5 s while bridges started.
- One 9.5 s fetch stall coincided with a bridge restart and expired one legacy Party reply (`expired_before_delivery`). It did not recur in later runs.
- Fetches are asynchronous, so the world thread was not blocked.

## Benchmarks (bridge engine, simulated realm, no network or database)

`bench_chatter2_full.py` runs 120 simulated seconds per scale, with:

- a mixed event stream: combat, kill, loot, quest, Party, General, guild, /say, bot-to-bot replies, BG, raid, world events and whispers;
- ambient and idle initiation ticks;
- simulated 2 ms knowledge lookups;
- `call_llm` against a failing provider (circuit breaker: 5 failures / 30 s).

| Bots | Events | Lifecycle-quiet (no model call) | Repeat openers dropped | Engine p50 / p95 / p99 µs | Engine CPU (one core) | Scopes | Peak mem |
|---|---|---|---|---|---|---|---|
| 1,500 | 897 | 1,751 | 55 | 70 / 2,176 / 2,478 | 0.22% | 432 | 2.5 MB |
| 2,000 | 1,227 | 2,415 | 69 | 59 / 2,155 / 2,206 | 0.28% | 571 | 1.7 MB |
| 3,000 | 1,854 | 3,641 | 118 | 63 / 2,163 / 2,225 | 0.45% | 878 | 2.3 MB |
| 4,000 | 2,481 | 4,868 | 125 | 59 / 2,161 / 2,295 | 0.59% | 1,153 | 3.0 MB |

- **Latency.** p95 and p99 are the simulated 2 ms knowledge round trip on the few events with a quest ID or intent. Without a lookup the engine costs about 60 µs.
- **30% provider failure.** 858–1,311 requests were sent and 15–1,232 skipped by the breaker or deadline, depending on failure streaks.
- **Provider outage (100% failure).** Only **20 requests in 120 s** at every scale (the breaker). All other events were skipped with no provider traffic.
- **Not measured.** The world-thread cost of the C++ capture at 3–4k live bots has not been profiled on a populated server. It is unchanged from 2.0 and bounded to ≤ 8 bots per turn.

## Real-model evaluation

`tools/tests/integration/real_model_eval.py` covers 14 scenarios across all surfaces, 3 runs each. It scores:

- assistant voice;
- ungrounded facts;
- unsupported live claims;
- markdown or lists;
- repetition;
- latency p50/p95.

**Status: COMPLETED (authorised by the user, 2026-11-08).**

- **Run.** 42 paid requests: 14 scenarios × 3 runs, OpenAI `gpt-4o-mini` (the production model), using the key from the installed config. The key was passed only to the eval process, never printed, and does not appear in any output file.
- **Prompts.** Reactive scenarios use the candidate's `build_reply_prompt`. The other surfaces go through `call_llm` with the shared surface engine. Knowledge lookups were off, so only the evidence placed in each scenario was available.
- **Outputs.** In `C:\WHE-Staging\Chatter2\eval_out_run1\`: `prompts.json`, `replies.json` and `report.json`.

| Measure (42 replies) | Result |
|---|---|
| Empty or invalid replies | 0 |
| Assistant voice ("as an AI", "happy to help", …) | 0 |
| Markdown or lists | 0 |
| Linked quest with DATA ("who starts [A Threat Within]?") | 3/3 correct: "deputy willem in elwynn forest." |
| Readiness with low-mana evidence ("heals ready?") | 1/3 mentioned low mana. 2/3 said "got some heals", which is not false but ignores the evidence |
| **Ungrounded drop sources (no DATA)** | **6/6.** Linen cloth: "humanoids … westfall or the barrens", "gnolls or murlocs". Wool: "from sheep in westfall / the wetland area / hillsbrad". Wool does not drop from sheep. Some replies hedge ("i think", "might"), but all state a source. |
| **Invented own stats** | **2/27** non-reactive replies stated a mana percentage the prompt never gave: combat "i'm at 70% mana", raid pull "i'm at 60% mana" |
| Lore/geography slips (manual review) | 1/3 ambient lines: "the worgen near amberstill ranch" (Amberstill Ranch is in Dun Morogh; the prompt was Elwynn) |
| Repetition | Whisper "hey you free later?" got the identical reply 3/3. Other surfaces 0–33% |
| Latency, all 42 | p50 677 ms, p95 989 ms, max 1,956 ms |

**Interpretation**

- **Tone and format.** Register, persona, length and channel fit are good on every surface.
- **Grounding with DATA.** It works when DATA is present.
- **Grounding without DATA.** The model does not follow the "no specific drop sources without DATA" rule. This affects reactive Party and General in this run. In production, an item link or a recognised item name would trigger a knowledge lookup and supply DATA; plain-text names that are not recognised fall back to the model's guesses.
- **Self-stat claims.** The existing `unsupported_claim` guard only covers reactive replies and low-power wording. It does not catch "70% mana" on event surfaces. A guard for numeric self-state on all surfaces is recommended before production (§9 of the deployment plan).

The scorer was tightened after a first pass undercounted: it now catches numeric self-stats and hedged drop claims. The saved replies were rescored offline with `--rescore`, with no extra requests.


### Grounding guards and focused re-run (after the first evaluation)

**Guards added to the shared engine.** They apply on every surface, reactive and event, and are inert under `Pipeline=1`.

- **`SOURCE_RULE`.**
  - **When it applies.** The player asks where something drops, comes from, or is sold, and DATA has no found or partial `loot` record.
  - **What it does.** It adds a system rule that forbids naming mobs, creature types, zones, NPCs or vendors. The model is told to say it is not sure, in its own voice.
- **`check_reply` in `call_llm`.** It discards a reply that falls into either case below. A discarded reply behaves like a failed request: silence, or the reactive path's evidence-only fallback.
  - **`invented_self_stat`.**
    - A health or mana percentage the prompt never gave.
    - A word-form own state such as "i'm at half health", "low on hp" or "full mana" when the prompt has no health or mana state.
    - Typographic apostrophes are normalised before this check.
  - **`ungrounded_source`.** The player asked where something comes from, DATA has no verified source, and the reply names one anyway.
- **Visibility.** The reason is recorded in `metadata['rejected']`, in the reactive trace as `rejected`, and as `skipped.llm.reply.<reason>` metrics.
- **Tests.** `tools/tests/test_chatter2_grounding.py` (22 tests) uses the exact failing replies from the evaluation, plus legitimate ones that must still pass:
  - evidence-backed "22% mana";
  - "45% health" when the prompt gives it;
  - "boss is at 20%";
  - "that dropped from vancleef" without a source question;
  - honest "not sure".

**Focused re-run.** 15 paid requests, authorised: the five previously failing scenarios × 3, same model and key handling. Outputs are in `C:\WHE-Staging\Chatter2\eval_out_run2\`, where `replies.json` records both what the model said and what would be sent.

| Scenario | Before guards (run 1) | Model output with `SOURCE_RULE` (run 2) | Sent with the final guards (replayed offline) |
|---|---|---|---|
| "where does linen cloth drop" (no DATA) | 3/3 named a source | 1/3 "not sure, might wanna check a resource"; 2/3 "not sure, maybe from humanoids…" | 1 honest "not sure"; **2 discarded (`ungrounded_source`)** |
| "anyone know where the wool drops?" (no DATA) | 3/3 named a source (sheep) | **3/3 "not sure", suggests the AH or a guide** | 3 sent, all honest |
| Combat (no state in prompt) | 1/3 "i'm at 70% mana" | 0/3 claims | 3 sent |
| Raid pull (no state in prompt) | 1/3 "i'm at 60% mana" | 2/3 "i'm at half health" | **2 discarded (`invented_self_stat`)**, 1 sent |
| "heals ready?" (low-mana evidence, 22%) | 1/3 mentioned low mana | 3/3 used it ("got 22% mana", "need some mana back") | 3 sent (evidence-backed) |

- **Run 2 as sent live.** It was sent before the word-form check existed, and delivered the two "half health" lines.
- **Replay with the final code.** Replaying run 2 through the final code sends **0 guessed sources and 0 invented stats.**
- **Latency.** p50 861 ms, max 4,326 ms (one slow provider response).
- **Trade-off.** Guarded turns become silence. A player asking a direct "where does X drop?" with no data now gets either an honest "not sure" or no reply.
