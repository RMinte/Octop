-- Replace manual business labels with categories derived from the publication name.
ALTER TABLE project_task_artifacts
    DROP CONSTRAINT IF EXISTS project_task_artifacts_business_category_check;
ALTER TABLE project_task_artifacts ALTER COLUMN business_category SET DEFAULT 'other';
WITH categorized AS (
    SELECT id, translate(name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz') AS category_name
    FROM project_task_artifacts
)
UPDATE project_task_artifacts SET business_category =
CASE
    WHEN substr(categorized.category_name,length(categorized.category_name)-2+1) IN ('.c', '.h', '.r') THEN 'document'
    WHEN substr(categorized.category_name,length(categorized.category_name)-3+1) IN ('.md', '.py', '.js', '.ts', '.sh', '.rs', '.go', '.kt', '.cc', '.cs', '.rb') THEN 'document'
    WHEN substr(categorized.category_name,length(categorized.category_name)-4+1) IN ('.pdf', '.doc', '.odt', '.rtf', '.txt', '.log', '.rst', '.pyi', '.jsx', '.mjs', '.cjs', '.tsx', '.yml', '.htm', '.css', '.xml', '.ini', '.cfg', '.zsh', '.ps1', '.bat', '.cmd', '.sql', '.kts', '.cpp', '.hpp', '.php', '.vue', '.lua') THEN 'document'
    WHEN substr(categorized.category_name,length(categorized.category_name)-5+1) IN ('.docx', '.adoc', '.json', '.yaml', '.html', '.scss', '.sass', '.less', '.toml', '.conf', '.bash', '.java', '.dart') THEN 'document'
    WHEN substr(categorized.category_name,length(categorized.category_name)-6+1) IN ('.pages', '.swift', '.jsonl', '.ipynb') THEN 'document'
    WHEN substr(categorized.category_name,length(categorized.category_name)-7+1) IN ('.ndjson', '.svelte') THEN 'document'
    WHEN substr(categorized.category_name,length(categorized.category_name)-9+1) IN ('.markdown') THEN 'document'
    WHEN substr(categorized.category_name,length(categorized.category_name)-4+1) IN ('.ppt', '.odp', '.key') THEN 'presentation'
    WHEN substr(categorized.category_name,length(categorized.category_name)-5+1) IN ('.pptx') THEN 'presentation'
    WHEN substr(categorized.category_name,length(categorized.category_name)-4+1) IN ('.csv', '.tsv', '.xls', '.ods') THEN 'spreadsheet'
    WHEN substr(categorized.category_name,length(categorized.category_name)-5+1) IN ('.xlsx') THEN 'spreadsheet'
    WHEN substr(categorized.category_name,length(categorized.category_name)-8+1) IN ('.numbers') THEN 'spreadsheet'
    WHEN substr(categorized.category_name,length(categorized.category_name)-4+1) IN ('.png', '.jpg', '.gif', '.svg', '.bmp', '.ico', '.tif') THEN 'image'
    WHEN substr(categorized.category_name,length(categorized.category_name)-5+1) IN ('.jpeg', '.webp', '.heic', '.heif', '.avif', '.tiff') THEN 'image'
    ELSE 'other' END
FROM categorized WHERE project_task_artifacts.id=categorized.id;
ALTER TABLE project_task_artifacts
    ADD CONSTRAINT project_task_artifacts_business_category_suffix_check
    CHECK (business_category IN ('document', 'presentation', 'spreadsheet', 'image', 'other'));
UPDATE _schema_version SET version = 49;
