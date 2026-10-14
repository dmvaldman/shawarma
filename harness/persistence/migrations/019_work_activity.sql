ALTER TABLE coding_sessions ADD COLUMN current_tool text;
ALTER TABLE coding_sessions ADD COLUMN current_branch text;
ALTER TABLE coding_sessions ADD COLUMN current_commit text;
ALTER TABLE coding_sessions ADD COLUMN branch_observed_at timestamptz;
ALTER TABLE tool_calls ADD COLUMN started_at timestamptz NOT NULL DEFAULT now();
