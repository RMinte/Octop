-- Account policy replaces per-task choices. Preserve all historical action/turn snapshots.
CREATE TABLE user_task_approval_policies (
    owner_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('ask_for_approval','approve_for_me','full_access')),
    revision BIGINT NOT NULL CHECK (revision >= 1),
    updated_at BIGINT NOT NULL,
    requests_json TEXT NOT NULL DEFAULT '[]'
);
-- Start safely and advance beyond every old per-task version so cached values cannot win.
INSERT INTO user_task_approval_policies(owner_id,mode,revision,updated_at,requests_json)
SELECT u.id, 'ask_for_approval', COALESCE(MAX(t.approval_revision), 0) + 1,
       CASE WHEN MAX(t.approval_updated_at) > u.created_at * 1000
            THEN MAX(t.approval_updated_at) ELSE u.created_at * 1000 END, '[]'
FROM users u LEFT JOIN project_tasks t ON t.owner_id=u.id
GROUP BY u.id, u.created_at;
UPDATE _schema_version SET version = 45;
