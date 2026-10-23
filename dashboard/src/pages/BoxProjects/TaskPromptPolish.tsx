import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Modal } from "antd";
import { useTranslation } from "react-i18next";
import { useTaskHost } from "../../context/TaskHostContext";
import { apiErrorMessage } from "../../utils/apiError";

export default function TaskPromptPolish({
  text,
  taskID,
  expertID,
  modelRef,
  disabled,
  onApply,
}: {
  text: string;
  taskID?: string;
  expertID: string | null;
  modelRef?: string;
  disabled: boolean;
  onApply: (text: string) => void;
}) {
  const { t } = useTranslation();
  const { taskExtrasApi } = useTaskHost();
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<string>();
  const [result, setResult] = useState<string>();
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const polish = async () => {
    const original = text;
    setBusy(true);
    setError("");
    try {
      const value = await taskExtrasApi.polish({
        text: original,
        task_id: taskID,
        selected_expert_id: expertID,
        model_ref: modelRef,
      });
      if (alive.current) {
        setSource(original);
        setResult(value.text);
      }
    } catch (error) {
      if (alive.current)
        setError(apiErrorMessage(error, t("projects.tasks.failure"), t));
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  return (
    <>
      <Button
        size="small"
        disabled={disabled || !text.trim()}
        loading={busy}
        onClick={() => void polish()}
      >
        {t("projects.tasks.polish.title")}
      </Button>
      {error && (
        <Alert
          type="error"
          message={error}
          closable
          onClose={() => setError("")}
        />
      )}
      <Modal
        title={t("projects.tasks.polish.review")}
        open={result !== undefined}
        onCancel={() => setResult(undefined)}
        okText={t("projects.tasks.polish.apply")}
        okButtonProps={{ disabled: source !== text || !result?.trim() }}
        onOk={() => {
          if (source === text && result) {
            onApply(result);
            setResult(undefined);
          }
        }}
      >
        {source !== text && (
          <Alert type="warning" message={t("projects.tasks.polish.changed")} />
        )}
        <Input.TextArea
          aria-label={t("projects.tasks.polish.review")}
          value={result}
          onChange={(event) => setResult(event.target.value)}
          rows={10}
        />
      </Modal>
    </>
  );
}
