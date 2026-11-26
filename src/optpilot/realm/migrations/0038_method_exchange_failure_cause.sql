-- A failed method exchange records why it failed.
--
-- Until now a method failure stored a code and two digests and nothing else,
-- so a Run that stopped because its method broke could not say what broke.
-- The cause is reduced before it is written -- frames dropped, absolute paths
-- substituted, length bounded -- so this column keeps the stream's promise
-- that it holds no tracebacks and no host paths.
--
-- Rows written before this column existed read NULL, which means "no detail
-- retained", not "no detail existed".
ALTER TABLE run_method_exchange_completions ADD COLUMN error_json TEXT CHECK(
    error_json IS NULL OR (
        length(CAST(error_json AS BLOB)) BETWEEN 2 AND 4096
        AND json_valid(error_json)
        AND json_type(error_json) = 'object'
        AND error_json = json(error_json)
    )
);
