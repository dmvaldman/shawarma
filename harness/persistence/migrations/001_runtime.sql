CREATE TABLE workspaces (
 id uuid PRIMARY KEY, tenant_id uuid, name text NOT NULL, state text NOT NULL DEFAULT 'active',
 seed_id uuid NOT NULL, server_id text NOT NULL, team_id text NOT NULL, general_id text NOT NULL,
 owner_id text NOT NULL, created_ms bigint NOT NULL, question text, started boolean NOT NULL DEFAULT false,
 budget numeric NOT NULL CHECK(budget>0), spent numeric NOT NULL DEFAULT 0,
 reserved numeric NOT NULL DEFAULT 0, limits jsonb NOT NULL
);
CREATE TABLE agents (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL REFERENCES workspaces(id),
 parent_id uuid REFERENCES agents(id), identity jsonb NOT NULL, model_id text NOT NULL,
 state text NOT NULL DEFAULT 'IDLE', availability text NOT NULL DEFAULT 'available'
 CHECK(availability IN ('available','away')), budget numeric NOT NULL CHECK(budget>0),
 spent numeric NOT NULL DEFAULT 0, reserved numeric NOT NULL DEFAULT 0,
 turns integer NOT NULL DEFAULT 0, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION immutable_agent_birth() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.identity IS DISTINCT FROM OLD.identity OR NEW.parent_id IS DISTINCT FROM OLD.parent_id
 OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR NEW.model_id IS DISTINCT FROM OLD.model_id
 THEN RAISE EXCEPTION 'agent birth record is immutable'; END IF; RETURN NEW;
END $$;
CREATE TRIGGER immutable_birth BEFORE UPDATE ON agents FOR EACH ROW EXECUTE FUNCTION immutable_agent_birth();
CREATE TABLE subscriptions (
 agent_id uuid REFERENCES agents(id), channel_id text NOT NULL, mode text NOT NULL
 CHECK(mode IN ('all','mentions','none')), PRIMARY KEY(agent_id,channel_id)
);
CREATE FUNCTION mandatory_general() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE general text;
BEGIN
 SELECT w.general_id INTO general FROM workspaces w JOIN agents a ON a.workspace_id=w.id
 WHERE a.id=OLD.agent_id;
 IF OLD.channel_id=general AND (TG_OP='DELETE' OR NEW.mode!='all' OR NEW.channel_id!=general)
 THEN RAISE EXCEPTION 'general subscription is mandatory'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER general_subscription BEFORE UPDATE OR DELETE ON subscriptions
 FOR EACH ROW EXECUTE FUNCTION mandatory_general();
CREATE TABLE notifications (
 id uuid PRIMARY KEY, sequence bigserial UNIQUE, agent_id uuid REFERENCES agents(id),
 server_id text NOT NULL, post_id text NOT NULL, kind text NOT NULL, revision bigint NOT NULL DEFAULT 0,
 post_preview jsonb NOT NULL, state text NOT NULL DEFAULT 'unread'
 CHECK(state IN ('unread','reserved','consumed')), reserved_turn uuid,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(server_id,post_id,agent_id,kind,revision)
);
CREATE INDEX inbox ON notifications(agent_id,state,sequence);
CREATE TABLE message_cursors (
 agent_id uuid REFERENCES agents(id), server_id text NOT NULL, channel_id text NOT NULL,
 last_seen_at bigint NOT NULL, last_seen_post_id text NOT NULL,
 PRIMARY KEY(agent_id,server_id,channel_id)
);
CREATE TABLE conversations (
 sequence bigserial PRIMARY KEY, agent_id uuid REFERENCES agents(id), item jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE agent_turns (
 id uuid PRIMARY KEY, agent_id uuid REFERENCES agents(id), request jsonb NOT NULL,
 state text NOT NULL DEFAULT 'prepared', provider_request_id text, output jsonb,
 input_tokens bigint, output_tokens bigint, cost numeric,
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
);
CREATE TABLE usage_reservations (
 id uuid PRIMARY KEY REFERENCES agent_turns(id), agent_id uuid REFERENCES agents(id),
 workspace_id uuid REFERENCES workspaces(id), amount numeric NOT NULL CHECK(amount>0),
 state text NOT NULL DEFAULT 'reserved'
);
CREATE TABLE tool_calls (
 id uuid PRIMARY KEY, turn_id uuid REFERENCES agent_turns(id), agent_id uuid REFERENCES agents(id),
 call_id text NOT NULL, tool_name text NOT NULL, arguments jsonb NOT NULL, result jsonb,
 state text NOT NULL DEFAULT 'pending', UNIQUE(agent_id,call_id)
);
CREATE TABLE agent_tool_versions (
 agent_id uuid REFERENCES agents(id), name text NOT NULL, version integer NOT NULL,
 manifest jsonb NOT NULL, python_source text, enabled boolean NOT NULL DEFAULT true,
 PRIMARY KEY(agent_id,name,version)
);
CREATE TABLE events (
 sequence bigserial PRIMARY KEY, workspace_id uuid REFERENCES workspaces(id),
 actor_id uuid, type text NOT NULL, data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit/history is append-only'; END $$;
CREATE TRIGGER event_append_only BEFORE UPDATE OR DELETE ON events FOR EACH ROW EXECUTE FUNCTION append_only();
CREATE TRIGGER conversation_append_only BEFORE UPDATE OR DELETE ON conversations FOR EACH ROW EXECUTE FUNCTION append_only();
