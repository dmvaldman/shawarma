ALTER TABLE agents DROP COLUMN turn_limit;
UPDATE workspaces SET limits=limits-'max_model_turns_per_agent';
