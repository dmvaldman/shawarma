CREATE TABLE agent_avatars (
    agent_id uuid PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'queued',
    image bytea,
    model text NOT NULL,
    usage jsonb,
    mattermost_applied boolean NOT NULL DEFAULT false,
    gitea_applied boolean NOT NULL DEFAULT false,
    retry_at timestamptz NOT NULL DEFAULT now(),
    error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
