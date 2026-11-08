-- Chatter 2.0: live-claim revalidation and normal-mode whispers.
--
-- 1. llm_chatter_messages.claim_json: volatile observations a reactive
--    reply relied on (for example "power_low;g=42;pt=0;th=35"). Delivery
--    re-reads live state and drops the line if a claim stopped holding.
--    NULL for every existing and non-reactive row (no claim to check).
-- 2. llm_chatter_events.event_type gains 'bot_player_whisper'.
--
-- Additive and idempotent. Rollback: the column and enum value may stay in
-- place for older binaries, which neither read the column nor produce the
-- event type.

SET @has_claim_json = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_chatter_messages'
    AND COLUMN_NAME = 'claim_json'
);

SET @sql = IF(
  @has_claim_json = 0,
  "ALTER TABLE `llm_chatter_messages` ADD COLUMN `claim_json` VARCHAR(512) DEFAULT NULL AFTER `addressee_npc_spawn_id`",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_bot_player_whisper = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_chatter_events'
    AND COLUMN_NAME = 'event_type'
    AND COLUMN_TYPE LIKE '%bot_player_whisper%'
);

SET @sql = IF(
  @has_bot_player_whisper = 0,
  CONCAT(
    "ALTER TABLE `llm_chatter_events` MODIFY COLUMN ",
    "`event_type` enum(",
    "'weather_change','holiday_start','holiday_end',",
    "'creature_death_boss','creature_death_rare',",
    "'creature_death_guard','player_enters_zone',",
    "'bot_pvp_kill','bot_level_up','bot_achievement',",
    "'bot_quest_complete','world_boss_spawn','rare_spawn',",
    "'transport_arrives','day_night_transition',",
    "'enemy_player_near','bot_loot_item','bot_group_join',",
    "'bot_group_kill','bot_group_death','bot_group_loot',",
    "'bot_group_player_msg','bot_group_general_reaction',",
    "'bot_group_combat','bot_group_levelup',",
    "'bot_group_quest_complete','bot_group_achievement',",
    "'bot_group_spell_cast','bot_group_quest_objectives',",
    "'bot_group_resurrect','bot_group_zone_transition',",
    "'bot_group_dungeon_entry','bot_group_wipe',",
    "'bot_group_corpse_run','player_general_msg','minor_event',",
    "'bot_group_low_health','bot_group_oom',",
    "'bot_group_aggro_loss','bot_group_quest_accept',",
    "'bot_group_quest_accept_batch','weather_ambient',",
    "'bot_group_nearby_object','bot_group_join_batch',",
    "'bg_match_start','bg_match_end','bg_pvp_kill',",
    "'bg_flag_picked_up','bg_flag_dropped','bg_flag_captured',",
    "'bg_flag_returned','bg_node_contested','bg_node_captured',",
    "'bg_score_milestone','bg_idle_chatter','bg_player_arrival',",
    "'raid_boss_pull','raid_boss_kill','raid_boss_wipe',",
    "'raid_idle_morale','bot_group_farewell',",
    "'bot_group_subzone_change','bot_group_emote_observer',",
    "'bot_group_emote_reaction',",
    "'bot_group_screenshot_observation','proximity_say',",
    "'proximity_conversation','proximity_reply',",
    "'proximity_player_say','proximity_player_conversation',",
    "'proximity_player_emote','proximity_boss_approach',",
    "'proximity_boss_player_say','bot_backstory_regen',",
    "'bot_tone_regen','guild_idle_chatter',",
    "'guild_player_message','guild_login_greeting',",
    "'bot_player_whisper') NOT NULL"
  ),
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
