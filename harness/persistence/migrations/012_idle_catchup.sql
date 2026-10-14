CREATE TABLE idle_channel_cursors (
 agent_id uuid REFERENCES agents(id), channel_id text NOT NULL,
 last_read_at bigint NOT NULL, PRIMARY KEY(agent_id,channel_id)
);
