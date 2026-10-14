CREATE TABLE first_deliveries (
 workspace_id uuid NOT NULL REFERENCES workspaces(id),
 post_id text NOT NULL,
 agent_id uuid REFERENCES agents(id),
 PRIMARY KEY(workspace_id,post_id)
);
ALTER TABLE notifications ADD COLUMN immediate_delivery boolean NOT NULL DEFAULT false;
