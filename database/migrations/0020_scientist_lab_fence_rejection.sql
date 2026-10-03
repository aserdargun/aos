PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
ALTER TABLE scientist_lab_actions ADD COLUMN rejection_reason TEXT
    CHECK(rejection_reason IS NULL OR rejection_reason IN ('human_rejected','stale_controller','approval_expired'));
DROP TRIGGER scientist_lab_action_immutable;
CREATE TRIGGER scientist_lab_action_immutable BEFORE UPDATE ON scientist_lab_actions
WHEN NEW.action_id IS NOT OLD.action_id OR NEW.run_id IS NOT OLD.run_id
  OR NEW.task_json IS NOT OLD.task_json OR NEW.action_json IS NOT OLD.action_json
  OR NEW.body IS NOT OLD.body OR NEW.expires_at IS NOT OLD.expires_at
  OR NEW.created_at IS NOT OLD.created_at
  OR NOT ((OLD.state='pending' AND NEW.state='approved' AND NEW.rejection_reason IS NULL)
       OR (OLD.state='pending' AND NEW.state='rejected' AND NEW.rejection_reason IN ('human_rejected','stale_controller','approval_expired'))
       OR (OLD.state='approved' AND NEW.state='rejected' AND NEW.approver IS OLD.approver AND NEW.rejection_reason IN ('stale_controller','approval_expired'))
       OR (OLD.state='approved' AND NEW.state='intent' AND NEW.approver IS OLD.approver AND NEW.rejection_reason IS OLD.rejection_reason)
       OR (OLD.state='intent' AND NEW.state='acknowledged' AND NEW.approver IS OLD.approver AND NEW.rejection_reason IS OLD.rejection_reason))
BEGIN SELECT RAISE(ABORT,'Lab action identity or transition differs'); END;
INSERT INTO schema_migrations VALUES(20,'scientist_lab_fence_rejection',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
