import { useEffect, useState } from "react";
import { Button, Image, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";
import {
  projectTasksApi,
  type TaskInputAttachment,
} from "../../api/modules/projects";
import styles from "./index.module.less";

function TaskInputImage({ attachment }: { attachment: TaskInputAttachment }) {
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
  }, [attachment.attachment_id, attempt]);
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
      {attachments.map((attachment) => (
        <TaskInputImage
          key={attachment.attachment_id}
          attachment={attachment}
        />
      ))}
    </div>
  );
}
