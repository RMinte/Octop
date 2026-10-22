import { useMemo, useState } from "react";
import { Button, Drawer, Typography } from "antd";
import { FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { TaskFileChange } from "../../api/modules/projects";
import styles from "./TaskFileChanges.module.less";

interface ChangedFile {
  path: string;
  additions: number | null;
  deletions: number | null;
  diffs: string[];
  missingDiff: boolean;
}

function sumCounts(left: number | null, right: number | null) {
  return left == null || right == null ? null : left + right;
}

function groupChanges(changes: TaskFileChange[]) {
  const files = new Map<string, ChangedFile>();
  for (const change of changes) {
    const previous = files.get(change.path);
    if (previous) {
      previous.additions = sumCounts(previous.additions, change.additions);
      previous.deletions = sumCounts(previous.deletions, change.deletions);
      if (change.diff) previous.diffs.push(change.diff);
      previous.missingDiff ||= change.diff == null;
    } else {
      files.set(change.path, {
        path: change.path,
        additions: change.additions,
        deletions: change.deletions,
        diffs: change.diff ? [change.diff] : [],
        missingDiff: change.diff == null,
      });
    }
  }
  return [...files.values()];
}

function ChangeCounts({
  additions,
  deletions,
}: Pick<ChangedFile, "additions" | "deletions">) {
  const { t } = useTranslation();
  const addedLabel = t(
    additions == null
      ? "projects.tasks.fileChanges.unknownAdditions"
      : "projects.tasks.fileChanges.additions",
    { count: additions },
  );
  const deletedLabel = t(
    deletions == null
      ? "projects.tasks.fileChanges.unknownDeletions"
      : "projects.tasks.fileChanges.deletions",
    { count: deletions },
  );
  return (
    <span className={styles.counts}>
      <span
        className={styles.additions}
        aria-label={addedLabel}
        title={addedLabel}
      >
        +{additions == null ? "?" : additions.toLocaleString()}
      </span>
      <span
        className={styles.deletions}
        aria-label={deletedLabel}
        title={deletedLabel}
      >
        −{deletions == null ? "?" : deletions.toLocaleString()}
      </span>
    </span>
  );
}

function diffLineKind(line: string) {
  if (line.startsWith("+++ ") || line.startsWith("--- ")) return "meta";
  if (line.startsWith("+")) return "added";
  if (line.startsWith("-")) return "removed";
  if (line.startsWith("@@")) return "hunk";
  return "context";
}

export default function TaskFileChanges({
  changes,
}: {
  changes: TaskFileChange[];
}) {
  const { t } = useTranslation();
  const files = useMemo(() => groupChanges(changes), [changes]);
  const [expanded, setExpanded] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  if (!files.length) return null;
  const additions = files.reduce<number | null>(
    (total, file) => sumCounts(total, file.additions),
    0,
  );
  const deletions = files.reduce<number | null>(
    (total, file) => sumCounts(total, file.deletions),
    0,
  );
  const selected = files.find((file) => file.path === selectedPath);
  const visible = expanded ? files : files.slice(0, 3);
  const unknownCounts = additions == null || deletions == null;
  return (
    <section
      className={styles.card}
      aria-label={t("projects.tasks.fileChanges.title")}
    >
      <div className={styles.header}>
        <FileText size={24} className={styles.icon} aria-hidden="true" />
        <div className={styles.summary}>
          <Typography.Text strong>
            {t("projects.tasks.fileChanges.summary", { count: files.length })}
          </Typography.Text>
          <ChangeCounts additions={additions} deletions={deletions} />
          {unknownCounts && (
            <Typography.Text type="secondary">
              {t("projects.tasks.fileChanges.unknownCounts")}
            </Typography.Text>
          )}
        </div>
        <Button onClick={() => setSelectedPath(files[0].path)}>
          {t("projects.tasks.fileChanges.review")}
        </Button>
      </div>
      <ul className={styles.files}>
        {visible.map((file) => (
          <li key={file.path}>
            <button
              type="button"
              className={styles.file}
              onClick={() => setSelectedPath(file.path)}
            >
              <span className={styles.path}>{file.path}</span>
              <ChangeCounts
                additions={file.additions}
                deletions={file.deletions}
              />
            </button>
          </li>
        ))}
      </ul>
      {files.length > 3 && (
        <Button
          type="text"
          className={styles.expand}
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {t(
            expanded
              ? "projects.tasks.fileChanges.showLess"
              : "projects.tasks.fileChanges.showMore",
            { count: files.length - 3 },
          )}
        </Button>
      )}
      <Drawer
        open={selected != null}
        onClose={() => setSelectedPath(null)}
        title={t("projects.tasks.fileChanges.reviewTitle")}
        width={1000}
        destroyOnHidden
      >
        {selected && (
          <div className={styles.review}>
            <Typography.Text type="secondary">
              {t("projects.tasks.fileChanges.readOnly")}
            </Typography.Text>
            <nav
              className={styles.filePicker}
              aria-label={t("projects.tasks.fileChanges.title")}
            >
              {files.map((file) => (
                <Button
                  key={file.path}
                  type={file.path === selectedPath ? "primary" : "default"}
                  aria-current={file.path === selectedPath ? "true" : undefined}
                  onClick={() => setSelectedPath(file.path)}
                >
                  {file.path}
                </Button>
              ))}
            </nav>
            <div className={styles.reviewHeading}>
              <Typography.Text strong className={styles.path}>
                {selected.path}
              </Typography.Text>
              <ChangeCounts
                additions={selected.additions}
                deletions={selected.deletions}
              />
            </div>
            {selected.missingDiff && (
              <Typography.Paragraph type="secondary">
                {t("projects.tasks.fileChanges.missingDiff")}
              </Typography.Paragraph>
            )}
            {selected.diffs.length ? (
              <pre
                className={styles.diff}
                aria-label={t("projects.tasks.fileChanges.diffLabel", {
                  path: selected.path,
                })}
              >
                <code>
                  {selected.diffs
                    .join("\n\n")
                    .split("\n")
                    .map((line, index) => (
                      <span key={index} data-kind={diffLineKind(line)}>
                        {line}
                        {"\n"}
                      </span>
                    ))}
                </code>
              </pre>
            ) : (
              <Typography.Paragraph type="secondary">
                {t("projects.tasks.fileChanges.noDiff")}
              </Typography.Paragraph>
            )}
          </div>
        )}
      </Drawer>
    </section>
  );
}
