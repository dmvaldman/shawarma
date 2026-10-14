ALTER TABLE notifications ADD COLUMN available_at timestamptz NOT NULL DEFAULT now();
