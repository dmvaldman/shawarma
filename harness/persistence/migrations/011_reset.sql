-- Minimal operational receipts survive an explicit owner reset (no conversation data).
CREATE TABLE reset_requests (
 workspace_id uuid NOT NULL REFERENCES workspaces(id), request_id text NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','completed')),
 PRIMARY KEY(workspace_id,request_id)
);
