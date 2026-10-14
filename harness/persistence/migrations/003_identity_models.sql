CREATE TABLE agent_profiles (
 agent_id uuid PRIMARY KEY REFERENCES agents(id), username text NOT NULL, display_name text NOT NULL,
 role_name text NOT NULL, model_metadata jsonb NOT NULL
);
CREATE TABLE agent_prompt_versions (
 agent_id uuid REFERENCES agents(id), revision text NOT NULL, system_prompt text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(agent_id,revision)
);
CREATE TRIGGER prompt_version_append_only BEFORE UPDATE OR DELETE ON agent_prompt_versions
 FOR EACH ROW EXECUTE FUNCTION append_only();
ALTER TABLE spawn_requests ADD COLUMN bot_username text;
