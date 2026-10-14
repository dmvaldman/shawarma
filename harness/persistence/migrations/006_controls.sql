CREATE TABLE workspace_commands (
 workspace_id uuid NOT NULL REFERENCES workspaces(id),
 post_id text NOT NULL, posted_at bigint NOT NULL,
 command text NOT NULL CHECK(command IN ('@stop','@resume')),
 PRIMARY KEY(workspace_id,post_id)
);
ALTER TABLE agents ADD COLUMN turn_limit integer CHECK(turn_limit>0);
