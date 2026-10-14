CREATE TABLE channel_streams (
    id uuid PRIMARY KEY,
    agent_id uuid NOT NULL REFERENCES agents(id),
    call_id text NOT NULL,
    channel_id text NOT NULL,
    root_post_id text NOT NULL DEFAULT '',
    baseline jsonb NOT NULL,
    post_id text,
    message_id text,
    part_id text,
    text text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','completed','interrupted')),
    UNIQUE(agent_id,call_id)
);
CREATE UNIQUE INDEX one_open_channel_stream ON channel_streams(agent_id) WHERE state='open';
