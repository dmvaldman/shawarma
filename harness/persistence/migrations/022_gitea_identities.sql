CREATE TABLE gitea_identities (
 agent_id uuid PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
 username text NOT NULL UNIQUE, password text NOT NULL, token text,
 server_url text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
