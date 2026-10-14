-- Keep historical allocations for audit; new workers have no dollar allocation.
ALTER TABLE workspaces ALTER COLUMN budget DROP NOT NULL;
ALTER TABLE agents ALTER COLUMN budget DROP NOT NULL;
ALTER TABLE spawn_requests ALTER COLUMN budget DROP NOT NULL;
UPDATE workspaces SET limits=limits-'max_workspace_cost_usd'-'max_agent_cost_usd';
-- Retry previously blocked workers once; remaining context/capacity limits still apply.
UPDATE agents SET state='READY' WHERE state='QUOTA_BLOCKED' AND budget IS NOT NULL;
INSERT INTO events(workspace_id,type,data)
SELECT id,'owner_budget_limits_removed','{"usage_estimates_preserved":true}'::jsonb
FROM workspaces;
