ALTER TABLE agents ADD COLUMN birth_ms bigint NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN credential text;
ALTER TABLE agents ADD COLUMN allocation_released boolean NOT NULL DEFAULT false;
ALTER TABLE agents DROP CONSTRAINT agents_budget_check;
ALTER TABLE agents ADD CHECK(budget>=0);
CREATE TABLE spawn_requests (
 parent_id uuid REFERENCES agents(id), call_id text NOT NULL, child_id uuid NOT NULL UNIQUE,
 status text NOT NULL DEFAULT 'provisioning', budget numeric NOT NULL, request jsonb NOT NULL,
 created_ms bigint NOT NULL, PRIMARY KEY(parent_id,call_id)
);
