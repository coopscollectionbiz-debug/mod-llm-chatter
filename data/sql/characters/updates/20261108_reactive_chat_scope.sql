CREATE TABLE IF NOT EXISTS `llm_reactive_reply_turn` (
    `turn_key` VARBINARY(96) NOT NULL,
    `event_id` INT UNSIGNED NOT NULL,
    `audience` VARBINARY(512) NOT NULL,
    `bot_guid` INT UNSIGNED NOT NULL,
    `admitted_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`turn_key`),
    UNIQUE KEY `idx_reactive_event` (`event_id`),
    KEY `idx_reactive_cooldown` (`audience`, `admitted_at`),
    KEY `idx_reactive_admitted` (`admitted_at`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `llm_reactive_chat_history` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `audience` VARBINARY(512) NOT NULL,
    `speaker_guid` INT UNSIGNED NOT NULL,
    `speaker_name` VARCHAR(32) NOT NULL,
    `is_bot` TINYINT UNSIGNED NOT NULL,
    `message` TEXT NOT NULL,
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    KEY `idx_reactive_history` (`audience`, `id`),
    KEY `idx_reactive_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
