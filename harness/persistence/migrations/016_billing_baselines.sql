CREATE TABLE billing_baselines (
 workspace_id uuid NOT NULL REFERENCES workspaces(id), run_ms bigint NOT NULL,
 provider text NOT NULL, credential_hash text NOT NULL, kind text NOT NULL,
 period_start bigint NOT NULL, amount numeric NOT NULL,
 captured_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,run_ms,provider,credential_hash)
);
