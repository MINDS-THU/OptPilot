-- Filesystem device/inode values describe one mounted attachment.  They are
-- not durable root identity and may legitimately change after a reboot or
-- remount.  The private root marker and claim nonce provide durable identity;
-- descriptor-relative namespace code validates the current attachment before
-- activation.  Preserve the SQL boundary check that a volume wrapper and its
-- writable data directory remain on the same filesystem.

DROP TRIGGER ephemeral_volume_activation_anchor;

CREATE TRIGGER ephemeral_volume_activation_anchor
BEFORE UPDATE OF state ON ephemeral_volumes
WHEN NEW.state = 'active' AND NOT EXISTS (
    SELECT 1
    FROM ephemeral_volume_roots root
    JOIN leases usage ON usage.lease_id = NEW.usage_lease_id
    JOIN leases parent ON parent.lease_id = NEW.parent_lease_id
    WHERE root.volume_root_id = NEW.volume_root_id
      AND root.backend_kind = NEW.provider_kind
      AND root.state = 'active'
      AND NEW.wrapper_device_id = NEW.data_device_id
      AND usage.owner_id = NEW.owner_id
      AND usage.parent_lease_id = NEW.parent_lease_id
      AND usage.lease_kind = 'ephemeral-volume'
      AND usage.audience = parent.audience
      AND usage.scope_key = 'ephemeral-volume:' || NEW.volume_id
      AND usage.state = 'active'
      AND json_extract(usage.metadata_json, '$.volume_id') = NEW.volume_id
      AND json_extract(usage.metadata_json, '$.volume_root_id') = NEW.volume_root_id
      AND parent.owner_id = NEW.owner_id
      AND parent.state = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume activation is not anchored by its root and usage lease');
END;
