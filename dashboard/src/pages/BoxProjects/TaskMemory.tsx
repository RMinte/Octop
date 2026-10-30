import { randomUUID } from "../../utils/randomUUID";
import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Input,
  Modal,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import { useTaskHost } from "../../context/TaskHostContext";
import type { TaskMemoryEntry } from "../../api/modules/taskExtras";
import { apiErrorMessage } from "../../utils/apiError";

export default function TaskMemory({
  taskID,
  readOnly = false,
}: {
  taskID: string;
  readOnly?: boolean;
}) {
  const { t } = useTranslation();
  const { taskExtrasApi } = useTaskHost();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<TaskMemoryEntry[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [content, setContent] = useState("");
  const [editing, setEditing] = useState<string>();
  const [version, setVersion] = useState(0);
  const request = useRef<string>();
  const deletes = useRef(new Map<string, string>());
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    void taskExtrasApi
      .memory(taskID, query, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setRows(value.entries);
          setEnabled(value.enabled);
          setError("");
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setError(apiErrorMessage(error, t("projects.tasks.failure"), t));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [open, taskID, query, version, taskExtrasApi, t]);
  const save = async () => {
    if (!content.trim() || busy) return;
    request.current ??= randomUUID();
    setBusy(true);
    setError("");
    try {
      await taskExtrasApi.saveMemory(taskID, {
        request_id: request.current,
        content,
        ...(editing ? { id: editing } : {}),
      });
      setContent("");
      setEditing(undefined);
      request.current = undefined;
      setVersion((v) => v + 1);
    } catch (error) {
      setError(apiErrorMessage(error, t("projects.tasks.failure"), t));
    } finally {
      setBusy(false);
    }
  };
  const remove = (entry: TaskMemoryEntry) =>
    Modal.confirm({
      title: t("projects.tasks.taskMemory.delete"),
      content: entry.content,
      okButtonProps: { danger: true },
      onOk: async () => {
        const id = deletes.current.get(entry.id) ?? randomUUID();
        deletes.current.set(entry.id, id);
        try {
          await taskExtrasApi.deleteMemory(taskID, entry.id, id);
          setVersion((v) => v + 1);
        } catch (error) {
          setError(apiErrorMessage(error, t("projects.tasks.failure"), t));
          throw error;
        }
      },
    });
  return (
    <>
      <Button size="small" onClick={() => setOpen(true)}>
        {t("projects.tasks.taskMemory.title")}
      </Button>
      <Drawer
        title={t("projects.tasks.taskMemory.title")}
        open={open}
        onClose={() => setOpen(false)}
        width={640}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Alert
            type="info"
            showIcon
            message={t("projects.tasks.taskMemory.hint")}
          />
          {!enabled && (
            <Alert
              type="warning"
              message={t("projects.tasks.taskMemory.disabled")}
            />
          )}
          {error && <Alert type="error" message={error} />}
          <Input.Search
            aria-label={t("projects.tasks.taskMemory.search")}
            value={q}
            onChange={(event) => setQ(event.target.value)}
            onSearch={setQuery}
            allowClear
          />
          {loading ? (
            <Spin />
          ) : (
            rows.map((entry) => (
              <Space
                key={entry.id}
                direction="vertical"
                style={{
                  width: "100%",
                  borderBottom: "1px solid var(--fn-border)",
                  paddingBottom: 12,
                }}
              >
                <Tag>
                  {t(
                    `projects.tasks.taskMemory.${
                      entry.editable ? "canonical" : "evidence"
                    }`,
                  )}
                </Tag>
                <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
                  {entry.content}
                </Typography.Paragraph>
                {entry.editable && !readOnly && (
                  <Space>
                    <Button
                      disabled={busy}
                      onClick={() => {
                        setEditing(entry.id);
                        setContent(entry.content);
                        request.current = undefined;
                      }}
                    >
                      {t("projects.tasks.workbench.edit")}
                    </Button>
                    <Button
                      danger
                      disabled={busy}
                      onClick={() => remove(entry)}
                    >
                      {t("projects.tasks.taskMemory.delete")}
                    </Button>
                  </Space>
                )}
              </Space>
            ))
          )}
          {!readOnly && (
            <>
              <Input.TextArea
                aria-label={t("projects.tasks.taskMemory.content")}
                value={content}
                onChange={(event) => {
                  setContent(event.target.value);
                  request.current = undefined;
                }}
                rows={5}
                disabled={busy}
              />
              <Space>
                <Button
                  type="primary"
                  onClick={() => void save()}
                  loading={busy}
                  disabled={!content.trim()}
                >
                  {t("projects.tasks.taskMemory.save")}
                </Button>
                {editing && (
                  <Button
                    disabled={busy}
                    onClick={() => {
                      setEditing(undefined);
                      setContent("");
                      request.current = undefined;
                    }}
                  >
                    {t("projects.cancel")}
                  </Button>
                )}
              </Space>
            </>
          )}
        </Space>
      </Drawer>
    </>
  );
}
