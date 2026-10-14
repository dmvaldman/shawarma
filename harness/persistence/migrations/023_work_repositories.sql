CREATE TABLE work_repositories (
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name text NOT NULL,
    server_url text NOT NULL,
    owner_name text NOT NULL,
    marker text NOT NULL,
    repository_id bigint,
    PRIMARY KEY (workspace_id,name)
);
CREATE TABLE channel_repositories (
    workspace_id uuid NOT NULL,
    channel_id text NOT NULL,
    repository_name text NOT NULL,
    PRIMARY KEY (workspace_id,channel_id),
    FOREIGN KEY (workspace_id,repository_name) REFERENCES work_repositories(workspace_id,name)
      ON DELETE CASCADE
);
CREATE TABLE agent_repositories (
    agent_id uuid PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    repository_name text NOT NULL
);
