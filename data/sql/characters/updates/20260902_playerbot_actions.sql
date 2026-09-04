-- --------------------------------------------------------
-- Natural-language PlayerBot action queue
-- --------------------------------------------------------
-- LLM interpretation only creates canonical requests.
-- C++ validates proximity/state and executes allowed PlayerBots actions.

-- Natural-language PlayerBot action requests.
-- Python may classify an intent and enqueue a canonical request here.
-- C++ remains authoritative for bot availability, map/range checks,
-- target validation, action allowlisting, and actual PlayerBots execution.
CREATE TABLE IF NOT EXISTS `llm_playerbot_actions` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `event_id` INT UNSIGNED DEFAULT NULL,
    `player_guid` INT UNSIGNED NOT NULL,
    `bot_guid` INT UNSIGNED NOT NULL,
    `group_id` INT UNSIGNED DEFAULT NULL,
    `source_channel` VARCHAR(32) NOT NULL,
    `action_key` VARCHAR(48) NOT NULL,
    `action_arg` VARCHAR(255) DEFAULT NULL,
    `target_type` VARCHAR(16) DEFAULT NULL,
    `target_guid` BIGINT UNSIGNED DEFAULT NULL,
    `target_name` VARCHAR(64) DEFAULT NULL,
    `status` VARCHAR(16) NOT NULL DEFAULT 'pending',
    `result_code` VARCHAR(32) DEFAULT NULL,
    `result_detail` VARCHAR(255) DEFAULT NULL,
    `expires_at` TIMESTAMP NULL DEFAULT NULL,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `claimed_at` TIMESTAMP NULL DEFAULT NULL,
    `executed_at` TIMESTAMP NULL DEFAULT NULL,
    PRIMARY KEY (`id`),
    KEY `idx_playerbot_action_pending`
        (`status`, `created_at`),
    KEY `idx_playerbot_action_bot`
        (`bot_guid`, `status`),
    KEY `idx_playerbot_action_event`
        (`event_id`),
    KEY `idx_playerbot_action_player`
        (`player_guid`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
