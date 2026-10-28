import { useEffect, useRef, useState } from "react";
import { Button, Typography } from "antd";
import { Download, FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ResidentReportArtifact } from "../../api/modules/resident";
import { useTaskHost } from "../../context/TaskHostContext";
import { formatBytes } from "../../utils/embeddingDownload";
import styles from "./index.module.less";

function ReportAttachment({
  taskID,
  artifact,
}: {
  taskID: string;
  artifact: ResidentReportArtifact;
}) {
  const { t } = useTranslation();
  const { projectTasksApi } = useTaskHost();
  const controllerRef = useRef<AbortController | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(
    () => () => controllerRef.current?.abort(),
    [taskID, artifact.artifact_id],
  );
  const download = async () => {
    const controller = new AbortController();
    controllerRef.current?.abort();
    controllerRef.current = controller;
    setDownloading(true);
    setFailed(false);
    try {
      const blob = await projectTasksApi.downloadArtifact(
        taskID,
        artifact.artifact_id,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      try {
        anchor.href = url;
        anchor.download = artifact.name;
        document.body.appendChild(anchor);
        anchor.click();
      } finally {
        anchor.remove();
        URL.revokeObjectURL(url);
      }
    } catch {
      if (!controller.signal.aborted) setFailed(true);
    } finally {
      if (!controller.signal.aborted) setDownloading(false);
    }
  };
  return (
    <li className={styles.reportAttachment}>
      <FileText size={18} />
      <div className={styles.reportAttachmentInfo}>
        <Typography.Text ellipsis title={artifact.name}>
          {artifact.name}
        </Typography.Text>
        <Typography.Text type="secondary">
          {formatBytes(artifact.byte_count)}
        </Typography.Text>
        {failed && (
          <Typography.Text type="danger" role="alert">
            {t("projects.tasks.artifactDownloadFailed")}
          </Typography.Text>
        )}
      </div>
      <Button
        size="small"
        icon={<Download size={14} />}
        loading={downloading}
        aria-label={`${t("common.download")} ${artifact.name}`}
        onClick={() => void download()}
      >
        {t("common.download")}
      </Button>
    </li>
  );
}

export default function ResidentReportAttachments({
  taskID,
  artifacts,
}: {
  taskID: string;
  artifacts: ResidentReportArtifact[];
}) {
  const { t } = useTranslation();
  if (!artifacts.length) return null;
  return (
    <ul
      className={styles.reportAttachments}
      aria-label={t("projects.tasks.artifacts")}
    >
      {artifacts.map((artifact) => (
        <ReportAttachment
          key={`${taskID}:${artifact.artifact_id}`}
          taskID={taskID}
          artifact={artifact}
        />
      ))}
    </ul>
  );
}
