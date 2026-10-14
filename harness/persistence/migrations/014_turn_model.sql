-- Actual call model may differ from its owning agent (shared OpenAI web search).
ALTER TABLE agent_turns ADD COLUMN provider_model_id text;
