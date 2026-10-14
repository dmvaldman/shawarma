-- Preserve provider cache reads/writes and other usage details without rewriting history.
ALTER TABLE agent_turns ADD COLUMN provider_usage jsonb NOT NULL DEFAULT '{}';
