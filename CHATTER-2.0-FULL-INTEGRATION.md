# Chatter 2.0: full-system integration (release candidate)

Branch `codex/chatter-2.0-full` (from `d4554c7`), worktree `C:\Azerothcore-dev\mod-llm-chatter-2.0-full`.
Playerbots companion: branch `codex/who-natural-language` (from `caafea53`), worktree `C:\Azerothcore-dev\mod-playerbots-who`.
Nothing is pushed, merged, deployed or applied to production.

## 1. One engine, specialised policies

Every model request in the bridge goes through `chatter_llm.call_llm`, so that is where the shared engine sits.

```
event / queue row / idle tick
  -> lifecycle gate        chatter_surfaces.admit_event / initiation_gate   (player precedence, pacing, reply chains)
  -> dispatch scope        chatter_surfaces.dispatch_context                (surface + audience scope for this work)
  -> specialised handler   unchanged prompt builders (group, raid, BG, guild, proximity, ambient, ...)
  -> call_llm
       -> circuit breaker + deadline skip          (2.0, unchanged)
       -> chatter_surfaces.apply                    surface rules, audience-filtered knowledge, RSS (policy-gated)
       -> chatter_relationships.enrich_prompt       relationship stance + scoped recall (unchanged)
  -> insert_chat_message
       -> chatter_surfaces.admit_line               repeated-opener suppression per scope
  -> C++ delivery                                    audience/claim revalidation (2.0, unchanged)
```

- `tools/chatter_surfaces.py` holds the registry. Each `SurfacePolicy` sets the channel, the evidence audience (public, party or private), the knowledge mode (none, intent, quest_id or handled), the RSS mode (none, builder or engine), the lifecycle class, dedup, the speaker cap and short rules. Every call_llm label and every registered event type maps to one policy. Tests enforce that no label or event falls through except the declared system labels.
- `tools/chatter_lifecycle.py` keeps bounded per-scope conversation state: General zone, party, guild, /say area, battleground, and whisper pair. It covers active conversation windows, player precedence, an initiation gap scaled by personality, a bot reply-chain cap, and near-duplicate detection. All checks are O(1) under one lock, with no database, network or world-thread work.
- The reactive Party, General and whisper paths keep the 2.0 reply pipeline (`chatter_reply`). They are marked `handled`, so nothing is added twice.
- Rollback: when `LLMChatter.Dialogue.Pipeline = 1`, `LLMChatter.Surfaces.Enable = 0`, or the mode is roleplay, the engine is inert. Prompts and gating are then exactly the legacy ones, and the voice line in `profile_instruction` is suppressed as well.

## 2. Coverage matrix (every surface)

The columns mean the following:

- **K**: knowledge mode. `handled` means the 2.0 reply pipeline already supplies it.
- **RSS**: `builder` means the existing builder attaches RSS; `engine` means the engine may add it under the RSS service's own interest and pacing gates.
- **Life**: lifecycle class.
- **Labels**: the number of call_llm labels mapped to the surface.

| Surface | Channel | Audience | K | RSS | Life | Dedup | Max speakers | Labels | Event types |
|---|---|---|---|---|---|---|---|---|---|
| `party_reactive` | party | party | handled | – | reactive | no | 1 | 1 | (reactive_reply under Pipeline 2) |
| `whisper` | whisper | private | handled | – | reactive | no | 1 | 1 | bot_player_whisper |
| `general_reactive` | general | public | handled | – | reactive | no | 1 | 1 | player_general_msg |
| `party_player_conv` | party | party | intent | – | reactive | no | 2 | 3 | bot_group_player_msg (legacy conv variants) |
| `guild_player` | guild | public | intent | – | reactive | no | 2 | 1 | guild_player_message |
| `proximity_player` | say | public | intent | – | reactive | no | 2 | 4 | proximity_player_say / _conversation / _emote |
| `general_ambient` | general | public | – | builder | initiation | yes | 4 | 3 | ambient queue (statement/conversation) |
| `general_followup` | general | public | – | – | reply_chain | yes | 2 | 1 | General follow-ups |
| `party_idle` | party | party | – | engine | initiation | yes | 4 | 2 | idle statement / conversation |
| `bot_question` | party | party | – | – | initiation | yes | 2 | 1 | bot-asked questions |
| `guild_idle` | guild | public | – | builder | initiation | yes | 3 | 1 | guild_idle_chatter |
| `guild_login` | guild | public | – | – | event | yes | 2 | 1 | guild_login_greeting |
| `proximity` | say | public | – | engine | initiation | yes | 3 | 3 | proximity_say, proximity_conversation |
| `proximity_reply` | say | public | – | – | reply_chain | yes | 2 | 1 | proximity_reply (bot-to-bot) |
| `combat` | party | party | – | – | event | yes | 1 | 10 | combat, low health, OOM, aggro loss, spell cast |
| `boss` | party | party | – | – | event | yes | 1 | 4 | kill, wipe, proximity_boss_* |
| `dungeon` | party | party | – | – | event | yes | 1 | 2 | dungeon entry |
| `group_event` | party | party | – | – | event | yes | 2 | 17 | death, rez, corpse run, level-up, achievement, loot, nearby object, zone/subzone |
| `quest` | party | party | quest_id | – | event | yes | 2 | 4 | quest accept (+batch), complete, objectives |
| `group_lifecycle` | party | party | – | – | event | yes | 2 | 4 | join (+batch), farewell, welcome/greeting, composition |
| `raid` | raid | party | – | – | event | yes | 2 | 1+prefix | raid_boss_pull/kill/wipe, raid_idle_morale |
| `battleground` | battleground | party | – | – | event | yes | 2 | prefix | bg_* (flag, node, score, match start/end, pvp kill, idle) |
| `world_event` | general | public | – | – | initiation | yes | 3 | 2 | weather, day/night, holidays, transports, minor events |
| `general_loot` | general | public | – | – | event | yes | 1 | 1 | bot_loot_item, bot_group_general_reaction |
| `zone` | general | public | – | – | event | yes | 1 | 1 | player_enters_zone (intrusion) |
| `emote` | party | party | – | – | event | yes | 1 | 2 | emote reaction / observer |
| `screenshot` | party | party | – | – | event | yes | 2 | 1 | screenshot observation (vision call is `system`) |
| `precache` | general | public | – | – | none | no | 1 | 1 | ambient pre-generation |
| `system` (exempt) | – | – | – | – | – | – | – | 6 | memory_generation, bot_tone, bot_backstory, guild_session_summary, find_addressed_bot, screenshot_vision |

Audience invariants are enforced by tests:

- General, /say and guild only ever receive `public` evidence.
- Whisper evidence is `private`.
- Party and raid may also use `party` evidence.
- Memory evidence is never rendered by the engine.

## 3. Bot-to-bot conversation lifecycle

- **Initiation is personality-based.** The initiation gap per scope is `InitiationGapSeconds × (1.5 − talkativeness/100)`, so talkative speakers start talk more often and quiet ones less. The existing `willing_to_speak` and `should_speak` still decide whether a given bot speaks at all.
- **Players take precedence.** Any real player line in a scope blocks bot initiation there for `PlayerQuietSeconds` (default 45 s). It also resets the bot reply chain, so a human always restarts the turn order.
- **One conversation per scope.** A started multi-speaker exchange owns its scope for `ConversationSeconds` (40 s). No second ambient conversation starts on top of it.
- **No infinite loops.** Bot-to-bot replies (`proximity_reply`, General follow-ups) are capped at `MaxReplyChain` (3) per 90 s window per scope. Rules for social surfaces also tell the model to end naturally, with no question in the closing line.
- **No repeats.** The first line of a dispatch is compared with the scope's last eight lines (Jaccard ≥ 0.6 over content words). On a match the whole dispatch is dropped before any party slot is reserved, so a conversation is never half-delivered.
- **Gated work never reaches the model.** Gated queue rows complete quietly. Gated events are marked `skipped`, the same as existing quiet paths, and gated idle ticks return as "nothing generated".

## 4. Playerbots `who` collision (narrow fix)

- **Root cause.** `ExternalEventHelper::ParseChatCommand` matches the longest leading trigger name. Any Party line beginning "who " therefore reaches `WhoAction`, which whispers "…playing with <master>". "Who starts this quest?" got a command whisper and no conversation.
- **Playerbots fix.** `WhoConversation::IsConversationalParam` (a new header) makes `WhoAction::Execute` decline a parameter whose first word continues a sentence. The 34 words are listed in the header (starts, drops, is, are, can, else, …).
  - Bare `who` (spec summary), `who <skill>`, `who <item>` and any item or quest link are unchanged.
  - No other Playerbots parsing is touched.
- **Chatter mirror.** `LLMChatterCommandPolicy.h` (C++ ingress) and `chatter_group.is_conversational_who_question` (bridge) treat exactly those lines as conversation.
  - `LLMChatter.PlayerChat.ConversationalWho` (default 1) can turn it off on an unpatched Playerbots build.
  - `@Name …` conversation still works; `@selector command` stays a command.
- **One case table.** `tools/tests/data/who_routing_cases.json` (18 cases) is executed against all three implementations: Python, the C++ chatter header and the Playerbots header. They are compiled standalone with MSVC and with GCC 15.2 `-Werror`.
- **Regression found and fixed.** The 2.0 social layer's extra `startswith('@')` test had silenced Patch 1's "@BotName …" conversation. It now relies on the selector-aware command parser.

## 5. Knowledge, personality, memory, RSS across surfaces

- **Knowledge.** Every surface uses the 2.0 `KnowledgeEngine`: a read-only pool, `MAX_EXECUTION_TIME` and a revision-keyed cache.
  - Player-text surfaces (guild, /say, legacy Party conversation) use intent detection on the player's own words.
  - Quest events use the exact quest ID from the C++ payload.
  - Public surfaces drop party-only kinds (boss loot by position, nearby NPC services).
  - The uncertainty statuses and `EVIDENCE_RULES` are attached unchanged, bounded by `LLMChatter.Surfaces.KnowledgeSeconds` (1.2 s).
- **Personality.** `profile_instruction` appends the same deterministic voice line that reactive replies use. The `PLAYER PROFILE … [guid=,v=]` prefix that relationship enrichment keys on is unchanged, and reactive prompts no longer show the line twice. No profile is regenerated or rewritten.
- **Relationships and memory.** These are unchanged and shared: `enrich_prompt` runs after the surface engine for every surface. The privacy audit (`test_chatter2_privacy.py`) covers every reader and writer:
  - Legacy `llm_bot_memories` recall happens only for the exact private pair. It is denied, without even running a query, in General, /say, guild, larger parties, or when membership is unknown.
  - Relationship writers accept only the dispatched input, from its real sender, to a bot that replied.
  - The only other `llm_bot_memories` reader is the operator log-viewer snapshot, a local file that never reaches a prompt.
- **RSS.** The existing builders keep their labels (ambient statement/conversation, guild idle). The engine may add RSS to party idle conversations and /say small talk, only through `chatter_rss.select_topic`, so the same personality-interest, appetite, article/topic/bot gaps and daily caps apply. Combat, boss, quest, raid, BG and reactive surfaces never get RSS.

## 6. Live world awareness

Live state is unchanged from 2.0. It comes from the C++ `BuildBotStateJson` and reactive `live` payloads captured on the world thread in bounded form (≤ 8 bots), and reactive claims are revalidated at delivery. The engine adds no world-thread work: knowledge lookups run on bridge worker threads through their own pool with a deadline.

## 7. Portability fix found by the GCC build

Patch 1's scoped delivery calls `Channel::CanSpeak`. That member exists only in the staging core, through commit `43e2449cf` ("Expose channel speech eligibility to scoped delivery"); the Release1 `source-next` core does not have it, so GCC failed.

`LLMChatterChannelCompat.h` detects the member at compile time:

- **Patched cores** keep the exact membership and mute check.
- **Unpatched cores** fall back to `Channel::Say`'s own enforcement. A muted bot's line is then refused by the core without a module drop reason, so the core patch remains recommended (see the deployment plan).

## 8. Other defects found by validation

- **Custom races excluded (affects Patch 1 and production).** Worgen (12) and Goblin (9) bots from mod-worgoblin were dropped from Party, General, guild and BG responder pools by hard-coded race lists. They are now central tables in `chatter_constants`, with a regression test.
- **Unsupported live claims.** A reply asserting low mana or a sighting without the speaker's own matching evidence went out with only an `alive` claim, so delivery could not catch it. `chatter_dialogue.unsupported_claim` now silences such replies; the `rejected` reason appears in the trace.
- **`@BotName …` conversation was silenced** by 2.0's social layer. Fixed.

## 9. Results

Full numbers are in `CHATTER-2.0-TEST-RESULTS.md` (the "Full integration" sections). In brief:

- **Unit tests.** 567 passed offline (571 with the disposable MySQL), 0 failed.
- **C++ policy tests.** Green on MSVC and on GCC 15.2 `-Werror`.
- **Builds.** Windows staging and Linux/GCC (`source-next` copy) both exit 0.
- **Real-client end-to-end runs.** A headless 3.3.5a client on an isolated realm passed 12/12 with a Human companion and 12/12 with a Worgen companion. The Patch 1 rollback on the migrated schema passed 6/6.
- **Benchmarks.** The bridge engine handles 1.5k–4k simulated bots at about 60 µs per event and ≤ 0.6% of one core. A provider outage is capped at 20 requests per 120 s.
- **Real-model evaluation (42 gpt-4o-mini requests).** Persona, format and grounding with DATA are good. Without DATA the model guessed drop sources 6/6 times, and it invented mana percentages in 2/27 event lines (see TEST-RESULTS).
