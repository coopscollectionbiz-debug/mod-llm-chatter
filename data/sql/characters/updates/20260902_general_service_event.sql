-- Add General Playerbot service discovery event.
-- Idempotent: preserves the currently installed ENUM and
-- appends only the new value when it is absent.

SET @event_column_type = (
    SELECT COLUMN_TYPE
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'llm_chatter_events'
      AND COLUMN_NAME = 'event_type'
    LIMIT 1
);

SET @has_general_service = (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'llm_chatter_events'
      AND COLUMN_NAME = 'event_type'
      AND COLUMN_TYPE LIKE '%player_general_service_request%'
);

SET @new_event_column_type = IF(
    @has_general_service = 0,
    CONCAT(
        LEFT(
            @event_column_type,
            CHAR_LENGTH(@event_column_type) - 1
        ),
        ",'player_general_service_request')"
    ),
    @event_column_type
);

SET @sql = IF(
    @has_general_service = 0,
    CONCAT(
        'ALTER TABLE `llm_chatter_events` ',
        'MODIFY COLUMN `event_type` ',
        @new_event_column_type,
        ' NOT NULL'
    ),
    'SELECT 1'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
