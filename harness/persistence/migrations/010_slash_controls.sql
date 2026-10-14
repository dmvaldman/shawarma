ALTER TABLE agents ADD COLUMN control_paused boolean NOT NULL DEFAULT false;
CREATE TABLE slash_commands (
 workspace_id uuid NOT NULL REFERENCES workspaces(id),
 trigger text NOT NULL, command_id text NOT NULL, token text NOT NULL,
 PRIMARY KEY(workspace_id,trigger)
);
CREATE TABLE slash_control_receipts (
 workspace_id uuid NOT NULL REFERENCES workspaces(id),
 request_id text NOT NULL, channel_id text NOT NULL, command text NOT NULL,
 result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,request_id)
);
