# Chatter 2.0: Test results

All results below were measured on 2026-11-08 on the development workstation. **No gameplay test with a WoW client was performed**, and no real model was called. Every model response came from a fake provider: in-process for unit tests, loopback HTTP for the staging smoke.

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
