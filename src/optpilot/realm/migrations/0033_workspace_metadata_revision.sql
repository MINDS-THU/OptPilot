ALTER TABLE managed_workspaces
ADD COLUMN metadata_revision INTEGER NOT NULL DEFAULT 1 CHECK(
    typeof(metadata_revision) = 'integer' AND metadata_revision > 0
);

CREATE TRIGGER managed_workspace_metadata_revision_update
BEFORE UPDATE OF title, metadata_revision ON managed_workspaces
WHEN NOT (
    (
        NEW.title = OLD.title
        AND NEW.metadata_revision = OLD.metadata_revision
    )
    OR (
        OLD.state = 'active'
        AND NEW.state = 'active'
        AND NEW.title <> OLD.title
        AND NEW.metadata_revision = OLD.metadata_revision + 1
    )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'managed workspace metadata revision must advance exactly once with its title'
    );
END;
