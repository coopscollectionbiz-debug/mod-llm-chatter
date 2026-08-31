SET @table_name = 'llm_group_bot_traits';

SET @column_exists = (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = @table_name
      AND COLUMN_NAME = 'bot_state_json'
);

SET @sql = IF(
    @column_exists = 0,
    'ALTER TABLE llm_group_bot_traits
        ADD COLUMN bot_state_json MEDIUMTEXT NULL
        AFTER travel_updated_at',
    'SELECT 1'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


SET @column_exists = (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = @table_name
      AND COLUMN_NAME = 'bot_state_updated_at'
);

SET @sql = IF(
    @column_exists = 0,
    'ALTER TABLE llm_group_bot_traits
        ADD COLUMN bot_state_updated_at TIMESTAMP NULL DEFAULT NULL
        AFTER bot_state_json',
    'SELECT 1'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;