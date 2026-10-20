-- Replace manual business labels with categories derived from the publication name.
CREATE TABLE project_task_artifacts_v49 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL REFERENCES project_tasks(task_id),
  turn_id TEXT NOT NULL REFERENCES project_task_turns(turn_id),
  tool_call_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  name TEXT NOT NULL,
  media_type TEXT NOT NULL,
  byte_count BIGINT NOT NULL CHECK(byte_count >= 0 AND byte_count <= 26214400),
  sha256 TEXT NOT NULL,
  source_path TEXT,
  content BLOB NOT NULL,
  created_at BIGINT NOT NULL,
  business_category TEXT NOT NULL DEFAULT 'other'
    CONSTRAINT project_task_artifacts_business_category_suffix_check
    CHECK (business_category IN ('document', 'presentation', 'spreadsheet', 'image', 'other')),
  UNIQUE(turn_id, tool_call_id)
);
INSERT INTO project_task_artifacts_v49 (id,artifact_id,task_id,turn_id,tool_call_id,request_hash,name,media_type,byte_count,sha256,source_path,content,created_at,business_category)
SELECT id,artifact_id,task_id,turn_id,tool_call_id,request_hash,name,media_type,byte_count,sha256,source_path,content,created_at,
CASE
    WHEN substr(category_name,length(category_name)-2+1) IN ('.c', '.h', '.r') THEN 'document'
    WHEN substr(category_name,length(category_name)-3+1) IN ('.md', '.py', '.js', '.ts', '.sh', '.rs', '.go', '.kt', '.cc', '.cs', '.rb') THEN 'document'
    WHEN substr(category_name,length(category_name)-4+1) IN ('.pdf', '.doc', '.odt', '.rtf', '.txt', '.log', '.rst', '.pyi', '.jsx', '.mjs', '.cjs', '.tsx', '.yml', '.htm', '.css', '.xml', '.ini', '.cfg', '.zsh', '.ps1', '.bat', '.cmd', '.sql', '.kts', '.cpp', '.hpp', '.php', '.vue', '.lua') THEN 'document'
    WHEN substr(category_name,length(category_name)-5+1) IN ('.docx', '.adoc', '.json', '.yaml', '.html', '.scss', '.sass', '.less', '.toml', '.conf', '.bash', '.java', '.dart') THEN 'document'
    WHEN substr(category_name,length(category_name)-6+1) IN ('.pages', '.swift', '.jsonl', '.ipynb') THEN 'document'
    WHEN substr(category_name,length(category_name)-7+1) IN ('.ndjson', '.svelte') THEN 'document'
    WHEN substr(category_name,length(category_name)-9+1) IN ('.markdown') THEN 'document'
    WHEN substr(category_name,length(category_name)-4+1) IN ('.ppt', '.odp', '.key') THEN 'presentation'
    WHEN substr(category_name,length(category_name)-5+1) IN ('.pptx') THEN 'presentation'
    WHEN substr(category_name,length(category_name)-4+1) IN ('.csv', '.tsv', '.xls', '.ods') THEN 'spreadsheet'
    WHEN substr(category_name,length(category_name)-5+1) IN ('.xlsx') THEN 'spreadsheet'
    WHEN substr(category_name,length(category_name)-8+1) IN ('.numbers') THEN 'spreadsheet'
    WHEN substr(category_name,length(category_name)-4+1) IN ('.png', '.jpg', '.gif', '.svg', '.bmp', '.ico', '.tif') THEN 'image'
    WHEN substr(category_name,length(category_name)-5+1) IN ('.jpeg', '.webp', '.heic', '.heif', '.avif', '.tiff') THEN 'image'
    ELSE 'other' END
FROM (SELECT a.*, lower(name) AS category_name FROM project_task_artifacts a);
-- Keep AUTOINCREMENT's high-water mark even when its former last row was removed.
UPDATE sqlite_sequence SET seq=MAX(seq, COALESCE(
  (SELECT seq FROM sqlite_sequence WHERE name='project_task_artifacts'), 0
)) WHERE name='project_task_artifacts_v49';
DROP TABLE project_task_artifacts;
ALTER TABLE project_task_artifacts_v49 RENAME TO project_task_artifacts;
CREATE INDEX idx_task_artifacts_turn ON project_task_artifacts(task_id, turn_id, id);
CREATE INDEX idx_task_artifacts_created
    ON project_task_artifacts(created_at DESC, artifact_id DESC);
CREATE INDEX idx_task_artifacts_task_created
    ON project_task_artifacts(task_id, created_at DESC, artifact_id DESC);
UPDATE _schema_version SET version = 49;
