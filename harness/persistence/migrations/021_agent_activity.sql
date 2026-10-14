CREATE TABLE agent_activity (
 sequence bigserial PRIMARY KEY,
 agent_id uuid NOT NULL REFERENCES agents(id),
 source_key text NOT NULL, kind text NOT NULL, phase text NOT NULL,
 summary text NOT NULL, payload jsonb NOT NULL,
 occurred_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activity_agent_page ON agent_activity(agent_id,sequence DESC);
CREATE TRIGGER activity_append_only BEFORE UPDATE OR DELETE ON agent_activity
 FOR EACH ROW EXECUTE FUNCTION append_only();
-- Mutable live projection; durable history above never rewrites a prior event.
CREATE TABLE agent_activity_sources (
 agent_id uuid NOT NULL REFERENCES agents(id), source_key text NOT NULL,
 kind text NOT NULL, phase text NOT NULL, summary text NOT NULL,
 fingerprint text NOT NULL, payload jsonb NOT NULL,
 occurred_at timestamptz NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(agent_id,source_key)
);
