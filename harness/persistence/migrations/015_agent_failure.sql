ALTER TABLE agents ADD COLUMN failure jsonb;
UPDATE agents SET state='QUOTA_BLOCKED',failure=jsonb_build_object(
 'category','legacy_error',
 'reason','Earlier execution stopped; the specific cause was not stored. Check historical #errors.',
 'recovery','Resolve the cause, then use /continue in the relevant channel.',
 'at',now()::text
) WHERE state='FAILED';
