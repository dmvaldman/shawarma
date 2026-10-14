-- Coding usage is learned from the remote harness after execution, not reserved dollars.
ALTER TABLE usage_reservations DROP CONSTRAINT usage_reservations_amount_check;
ALTER TABLE usage_reservations ADD CHECK(amount>=0);
CREATE TABLE coding_sessions (
 agent_id uuid PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
 session_id text NOT NULL, directory text NOT NULL, branch text NOT NULL,
 worker_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE coding_runs (
 id uuid PRIMARY KEY, agent_id uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
 call_id text NOT NULL, turn_id uuid REFERENCES agent_turns(id),
 state text NOT NULL DEFAULT 'running', result jsonb, baseline jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 UNIQUE(agent_id,call_id)
);
