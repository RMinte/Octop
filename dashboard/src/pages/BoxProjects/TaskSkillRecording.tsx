import { randomUUID } from "../../utils/randomUUID";
import { useTaskHost } from "../../context/TaskHostContext";
import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { type TaskWorkbenchScope } from "../../api/modules/taskWorkbench";
import { apiErrorMessage } from "../../utils/apiError";

export default function TaskSkillRecording({
  scope,
  hasProject,
}: {
  scope: TaskWorkbenchScope;
  hasProject: boolean;
}) {
  const { taskWorkbenchApi } = useTaskHost();
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [recordingID, setRecordingID] = useState<string | null>(null);
  const [generatedID, setGeneratedID] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [applied, setApplied] = useState(false);
  const [statusVersion, setStatusVersion] = useState(0);
  const requestID = useRef<string | null>(null);
  const generated = useRef(generatedID);
  generated.current = generatedID;
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void taskWorkbenchApi
      .recordingStatus(scope.task_id, scope.scope_revision)
      .then(async (status) => {
        if (!cancelled) {
          if (!status.ok)
            throw new Error(status.error ?? t("projects.tasks.failure"));
          setRecordingID(status.active?.recordingId ?? null);
          if (
            !status.active &&
            status.latestRecordingId &&
            !generated.current
          ) {
            const skill = await taskWorkbenchApi.recordingSkill(
              scope.task_id,
              status.latestRecordingId,
              scope.scope_revision,
            );
            if (!cancelled && skill.ok && skill.skillContent) {
              setGeneratedID(skill.recordingId);
              setContent(skill.skillContent);
              if (skill.skillName) setName(skill.skillName);
              setApplied(false);
            }
          }
        }
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scope.task_id, scope.scope_revision, statusVersion, t, taskWorkbenchApi]);
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      if (alive.current) {
        setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
        setStatusVersion((v) => v + 1);
      }
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  const start = () =>
    run(async () => {
      const result = await taskWorkbenchApi.startRecording(
        scope.task_id,
        name.trim(),
        scope.scope_revision,
      );
      if (!result.ok || !result.recordingId)
        throw new Error(result.error ?? t("projects.tasks.failure"));
      if (alive.current) {
        setRecordingID(result.recordingId);
        setGeneratedID(null);
        setContent("");
        setApplied(false);
        requestID.current = null;
      }
    });
  const stop = () =>
    run(async () => {
      if (!recordingID) return;
      const result = await taskWorkbenchApi.stopRecording(
        scope.task_id,
        recordingID,
        name.trim(),
        scope.scope_revision,
      );
      if (!result.ok || !result.skillContent)
        throw new Error(result.error ?? t("projects.tasks.failure"));
      if (alive.current) {
        setGeneratedID(result.recordingId ?? recordingID);
        setRecordingID(null);
        setContent(result.skillContent);
        if (result.skillName) setName(result.skillName);
        requestID.current = null;
      }
    });
  const apply = () =>
    run(async () => {
      if (!generatedID || !content.trim() || !name.trim()) return;
      requestID.current ??= randomUUID();
      await taskWorkbenchApi.applyRecording(scope.task_id, generatedID, {
        request_id: requestID.current,
        name: name.trim(),
        content,
        expected_scope_revision: scope.scope_revision,
      });
      if (alive.current) setApplied(true);
    });
  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Alert
        type="info"
        showIcon
        message={t("projects.tasks.workbench.recordingHint")}
        description={t(
          hasProject
            ? "projects.tasks.workbench.projectSkill"
            : "projects.tasks.workbench.taskSkill",
        )}
      />
      {error && <Alert type="error" showIcon message={error} />}
      <Input
        aria-label={t("projects.tasks.workbench.skillName")}
        placeholder={t("projects.tasks.workbench.skillName")}
        value={name}
        disabled={busy || applied}
        onChange={(event) => {
          setName(event.target.value);
          requestID.current = null;
        }}
      />
      <Space wrap>
        {recordingID ? (
          <>
            <Tag color="red">{t("projects.tasks.workbench.recording")}</Tag>
            <Button
              loading={busy}
              disabled={scope.read_only || !name.trim()}
              onClick={() => void stop()}
            >
              {t("projects.tasks.workbench.stopRecording")}
            </Button>
          </>
        ) : (
          <Button
            loading={loading || busy}
            disabled={scope.read_only || !name.trim()}
            onClick={() => void start()}
          >
            {t("projects.tasks.workbench.startRecording")}
          </Button>
        )}
        <Button disabled={busy} onClick={() => setStatusVersion((v) => v + 1)}>
          {t("projects.tasks.workbench.refresh")}
        </Button>
      </Space>
      {!!generatedID && (
        <>
          <Typography.Text strong>
            {t("projects.tasks.workbench.reviewSkill")}
          </Typography.Text>
          <Input.TextArea
            aria-label={t("projects.tasks.workbench.skillContent")}
            autoSize={{ minRows: 12, maxRows: 28 }}
            value={content}
            disabled={busy || applied}
            onChange={(event) => {
              setContent(event.target.value);
              requestID.current = null;
            }}
          />
          <Button
            type="primary"
            loading={busy}
            disabled={
              applied || scope.read_only || !name.trim() || !content.trim()
            }
            onClick={() => void apply()}
          >
            {t(
              applied
                ? "projects.tasks.workbench.skillApplied"
                : "projects.tasks.workbench.applySkill",
            )}
          </Button>
        </>
      )}
    </Space>
  );
}
