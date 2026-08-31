SET @dbname = DATABASE();
SET @tablename = 'llm_chatter_queue';
SET @columnname = 'bot_states_json';

SET @preparedStatement = (
    SELECT IF(
        (
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE
                TABLE_SCHEMA = @dbname
                AND TABLE_NAME = @tablename
                AND COLUMN_NAME = @columnname
        ) > 0,
        'SELECT 1',
        CONCAT(
            'ALTER TABLE ',
            @tablename,
            ' ADD COLUMN ',
            @columnname,
            ' MEDIUMTEXT NULL AFTER bot4_level'
        )
    )
);

PREPARE alterIfNeeded
FROM @preparedStatement;

EXECUTE alterIfNeeded;

DEALLOCATE PREPARE alterIfNeeded;