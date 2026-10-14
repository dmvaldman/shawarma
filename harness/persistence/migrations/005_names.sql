CREATE TABLE agent_name_reservations (
 server_id text NOT NULL, agent_id uuid NOT NULL, username text NOT NULL,
 PRIMARY KEY(server_id,username), UNIQUE(server_id,agent_id)
);
