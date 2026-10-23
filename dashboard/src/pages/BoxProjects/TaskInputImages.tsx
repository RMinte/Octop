import TaskMedia, { taskMediaKind } from "./TaskMedia";
import { useTaskHost } from "../../context/TaskHostContext";
import { useEffect, useRef, useState } from "react";
import { Button, Image, Spin, Typography } from "antd";
import { Download, FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import { type TaskInputAttachment } from "../../api/modules/projects";
import styles from "./index.module.less";
import { isTaskImage } from "./useTaskImages";

function TaskInputFile({ attachment }: { attachment: TaskInputAttachment }) {
  const { projectTasksApi } = useTaskHost();
  const { t } = useTranslation();
  const [downloading, setDownloading] = useState(false);
  const [failed, setFailed] = useState(false);
  const downloadController = useRef<AbortController | null>(null);
  useEffect(
    () => () => downloadController.current?.abort(),
    [attachment.attachment_id],
  );
  const download = async () => {
    const controller = new AbortController();
    downloadController.current?.abort();
    downloadController.current = controller;
    setDownloading(true);
    setFailed(false);
    try {
      const blob = await projectTasksApi.attachmentPreview(
        attachment.attachment_id,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = attachment.name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch {
      if (!controller.signal.aborted) setFailed(true);
    } finally {
      if (!controller.signal.aborted) setDownloading(false);
    }
  };
  return (
    <div className={styles.inputImage}>
      <div className={styles.inputFilePreview}>
        <FileText size={32} />
      </div>
      <Typography.Text ellipsis title={attachment.name}>
        {attachment.name}
      </Typography.Text>
      <Button
        size="small"
        icon={<Download size={14} />}
        loading={downloading}
        onClick={() => void download()}
      >
        {t("common.download")}
      </Button>
      {failed && (
        <Typography.Text type="danger" role="alert">
          {t("projects.tasks.artifactDownloadFailed")}
        </Typography.Text>
      )}
    </div>
  );
}

function TaskInputImage({ attachment }: { attachment: TaskInputAttachment }) {
  const { projectTasksApi } = useTaskHost();
  const { t } = useTranslation();
  const [url, setURL] = useState("");
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let previewURL = "";
    setURL("");
    setFailed(false);
    void projectTasksApi
      .attachmentPreview(attachment.attachment_id, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        previewURL = URL.createObjectURL(blob);
        setURL(previewURL);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => {
      controller.abort();
      if (previewURL) URL.revokeObjectURL(previewURL);
    };
  }, [attachment.attachment_id, attempt, projectTasksApi]);
  return (
    <div className={styles.inputImage}>
      {url ? (
        <Image src={url} alt={attachment.name} width={112} height={88} />
      ) : failed ? (
        <Button size="small" onClick={() => setAttempt((value) => value + 1)}>
          {t("projects.tasks.imagePreviewRetry")}
        </Button>
      ) : (
        <Spin size="small" />
      )}
      <Typography.Text ellipsis title={attachment.name}>
        {attachment.name}
      </Typography.Text>
    </div>
  );
}

export default function TaskInputImages({
  attachments,
}: {
  attachments: TaskInputAttachment[];
}) {
  if (!attachments.length) return null;
  return (
    <div className={styles.inputImages}>
      {attachments.map((attachment) =>
        taskMediaKind(attachment.media_type, attachment.name) ? (
          <TaskMedia key={attachment.attachment_id} attachment={attachment} />
        ) : isTaskImage(attachment.media_type) ? (
          <TaskInputImage
            key={attachment.attachment_id}
            attachment={attachment}
          />
        ) : (
          <TaskInputFile
            key={attachment.attachment_id}
            attachment={attachment}
          />
        ),
      )}
    </div>
  );
}
