# Chatter 2.0: Knowledge integration

## What was adapted from `mod-llm-guide`

Source: Hokken `mod-llm-guide` at `5fe0819eec25afd58a3c601e871f1de12f2f5ccd`, inspected read-only.

| Guide element | Used? | How |
|---|---|---|
| Exact-ID quest predicates (`guide_tool_quests.py` `_get_quest_info`) | Adapted | Rewritten as `lookup_quest_info` / `_quest_row`, keyed by `quest_template.ID` with an addon join |
| Creature + gameobject starter union (`_get_available_quests`) | Adapted | `lookup_quest_starters` unions creature and gameobject starters and item `startquest`, for starters and enders |
| Typed evidence / readiness (`guide_reliability.py`, `guide_readiness.py`) | Pattern adapted | `chatter_evidence.Evidence` with explicit statuses, plus the rule that missing evidence means "say you're not sure" |
| Command interface (`.ag`), multi-round tool loop, planner | **Not used** | Latency, cost and disclosure risk for one-line social chat |
| `_get_boss_loot` (fuzzy name, entry-as-lootid, parent reference chance) | **Not used** | Replaced by `lootid`-based lookup with two-level reference expansion and no computed chances |
| `_find_rare_spawn` (configured spawn counting) | **Not used** | Cannot establish that a rare is alive. Replaced by live `CanSeeOrDetect` observation. |
| Trainer/service area tables (`llm_guide_npc_areas`) | **Not used** | Replaced by a bounded box search around the bot's live position, labelled with straight-line distance only |
| Guide's prompt and character-context logging, Q&A memory | **Not used** | Chatter logs no prompts or private context by default |

**License (GNU AGPL v3).** No Guide source file was copied verbatim; the adapted query and evidence patterns were rewritten. Attribution, with the upstream commit and authors, is in the `chatter_knowledge.py` module docstring, and the commit message credits the upstream. Both modules ship AGPL v3. If deployed on a network service, the AGPL §13 corresponding-source obligation applies to the modified module, as it already did for this fork.

## Query behaviour

- **Connection.** Lazy, pooled (default 2), `SET SESSION TRANSACTION READ ONLY` and `MAX_EXECUTION_TIME=300` ms per connection. An optional least-privilege account is supported via `Knowledge.User/Password`.
- **Statements.** All parameterised. `LIMIT`s are on every statement. The turn's deadline is checked before each statement.
- **Caching.** LRU of 2048 entries for 900 s, keyed by `(world revision, intent, id, label)`. The revision is `COUNT(*)` plus `MAX(timestamp)` of `updates`, refreshed every 60 s, so a world DB update invalidates answers.
- **Per-turn cap.** At most 2 lookups (`Knowledge.MaxLookupsPerTurn`), and only for admitted turns.
- **Measured on the local dev world DB.** Read-only; this is not production:
  - starter lookup 2 ms
  - loot by item 1.2 ms
  - reference scan 5 ms
  - boss list 1.5–2.3 ms
  - spawn box search about 40 ms
  - every integration test lookup under 500 ms

## Supported questions

Recognition is by intent, not exact wording.

| Intent | Trigger | Result |
|---|---|---|
| `quest_starter` | Quest link plus who/where…start/get/pick up/give | Configured NPC, object or item starters, approximate spawn zone, `eligibility: not checked` |
| `quest_ender` | Quest link plus turn in/hand in | Configured enders |
| `quest_info` | Quest link plus level/req/prereq/chain/objective, or a bare question | Level, minimum level, resolved objectives, chain neighbours, condition count |
| `quest_eligibility` | Quest link plus can I take/accept | Always `unsupported`: needs character, progression and phase checks |
| `item_info` | Item link plus stats/good/upgrade/level, or a bare question | Quality, type, ilvl, required level, class restriction, quest starter |
| `item_source` | Item link plus drop/from/farm/where get | Creature, container and vendor sources; chance only for plain rows |
| `boss_drop` | Item link plus this boss/he drops | Membership on the unambiguous boss's table (difficulty-aware); absence becomes `partial` |
| `boss_list` | Which/how many bosses (no links) | Configured bosses on the party's current map |
| `npc_service` | Where/nearest plus trainer/vendor/repair/fp/inn/bank/ah/stable (no links) | Up to 3 configured NPCs by straight-line distance from the bot; class/profession filter |

## Evidence boundaries and limitations

- **Individual Progression.** IP gates quests through `conditions` rows plus C++ phasing and progression checks. The engine reports condition counts but can never decide eligibility. A private, core-side `CanTakeQuest` probe would be needed; that was out of scope and is deliberately not faked.
- **Plain-text quest and item names.** Never resolved, because same-title quests with different IDs exist. Native links only.
- **Forged links.** The link label is untrusted: the ID decides, and `link_label_mismatch` makes the speaker use the real title. Malformed links (no colour/terminator, non-numeric ID, pipe in label) are ignored.
- **Loot.** Scripted, encounter-chest and condition-driven drops cannot be fully resolved from tables, so every loot result carries a note that other sources may exist. Drop percentages through groups and references are never computed.
- **Spawn zones.** `creature.zoneId` is unpopulated for about 97% of spawns on this realm (5,219 of 155,205 rows), so zone names come from approximate bounding boxes and are labelled approximate.
- **Faction, phase and path to configured NPCs.** Not verified. The note says so.
- **Party phrasing that starts with "who".** It is the Playerbots `who` command, which Playerbots answers natively, so Chatter correctly stays out. "anyone know who starts …" works.
- **General.** Receives only linked quest/item `server_data`. Service and boss questions need a speaker position and stay unanswered there by design.
- **Rares.** Observed only around the asking player's party, within 80 yards (scan) and 100 yards (visibility), at ingress. A rare outside that radius is "not seen", never "down".
