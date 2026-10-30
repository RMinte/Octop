import { randomUUID } from "../../utils/randomUUID";
import { useTaskHost } from "../../context/TaskHostContext";
import { conversationUrl } from "../../api/taskTransport";
import { parseBridgeAgentId } from "../../utils/remoteExpert";
import { taskExpertLabel } from "./taskExpertLabel";
import { useEffect, useRef, useState } from "react";
import { Alert, Button, Modal, Select, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { type Project, type TaskResources } from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";
import { readInputDraft } from "../Chat/hooks/chatStore";
import { useTaskMessageQueue } from "./useTaskMessageQueue";

export default function LegacyConversationContinuation({
  agentID,
  threadID,
  busy,
}: {
  agentID: string;
  threadID: string | null;
  busy: boolean;
}) {
  const { projectsApi, projectTasksApi, hostID } = useTaskHost();
  const peerID = parseBridgeAgentId(agentID)?.remoteAgentId ?? agentID;
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queue = useTaskMessageQueue();
  const [open, setOpen] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [experts, setExperts] = useState<NonNullable<TaskResources["experts"]>>(
    [],
  );
  const [projectID, setProjectID] = useState<string | null>(null);
  const [expertID, setExpertID] = useState<string | null>(peerID);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<{ key: string; requestID: string } | null>(null);
  useEffect(() => {
    if (!open) return;
    let disposed = false;
    setLoading(true);
    void Promise.all([projectsApi.list(), projectTasksApi.resources()])
      .then(([rows, resources]) => {
        if (!disposed) {
          setProjects(rows);
          setExperts(resources.experts ?? []);
          setError("");
        }
      })
      .catch((err) => {
        if (!disposed)
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [open, projectTasksApi, projectsApi, t]);
  const continueHistory = async () => {
    if (!threadID || busy || saving || loading) return;
    const body = {
      agent_id: peerID,
      thread_id: threadID,
      project_id: projectID,
      selected_expert_id: expertID,
    };
    const key = JSON.stringify(body);
    if (request.current?.key !== key)
      request.current = { key, requestID: randomUUID() };
    setSaving(true);
    setError("");
    try {
      const result = await projectTasksApi.fromThread({
        ...body,
        request_id: request.current.requestID,
      });
      const draft = readInputDraft(agentID, threadID);
      if (draft) queue.drafts.current.set(result.task_id, draft);
      navigate(conversationUrl({ host_id: hostID, task_id: result.task_id }));
    } catch (err) {
      setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
    } finally {
      setSaving(false);
    }
  };
  const start = () => {
    if (threadID) {
      setOpen(true);
      return;
    }
    const draft = readInputDraft(agentID, null);
    if (draft) queue.drafts.current.set("draft:standalone", draft);
    navigate(
      `${conversationUrl(null, hostID)}${
        hostID ? "&" : "?"
      }expert_id=${encodeURIComponent(peerID)}`,
    );
  };
  return (
    <>
      <div style={{ padding: 16 }}>
        <Alert
          type="info"
          showIcon
          message={t("projects.tasks.legacy.readOnly")}
          description={t("projects.tasks.legacy.advancedHint")}
          action={
            <Button type="primary" disabled={busy} onClick={start}>
              {t(
                threadID
                  ? "projects.tasks.legacy.continue"
                  : "projects.tasks.legacy.start",
              )}
            </Button>
          }
        />
        {busy && (
          <Typography.Paragraph type="secondary">
            {t("projects.tasks.legacy.busy")}
          </Typography.Paragraph>
        )}
      </div>
      <Modal
        title={t("projects.tasks.legacy.continue")}
        open={open}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        onCancel={() => {
          if (!saving) setOpen(false);
        }}
        onOk={() => void continueHistory()}
        confirmLoading={saving}
        okButtonProps={{ disabled: loading || busy }}
        cancelButtonProps={{ disabled: saving }}
        closable={!saving}
        maskClosable={!saving}
      >
        <Typography.Paragraph>
          {t("projects.tasks.legacy.continueHint")}
        </Typography.Paragraph>
        {error && <Alert type="error" showIcon message={error} />}
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Text>{t("projects.tasks.project")}</Typography.Text>
          <Select
            aria-label={t("projects.tasks.branchProject")}
            style={{ width: "100%" }}
            loading={loading}
            disabled={saving}
            value={projectID ?? ""}
            options={[
              { value: "", label: t("projects.tasks.noProject") },
              ...projects
                .filter((project) => !project.archived)
                .map((project) => ({
                  value: project.project_id,
                  label: project.name,
                })),
            ]}
            onChange={(id) => setProjectID(id || null)}
          />
          <Typography.Text>{t("projects.tasks.expert")}</Typography.Text>
          <Select
            aria-label={t("projects.tasks.branchExpert")}
            style={{ width: "100%" }}
            loading={loading}
            disabled={saving}
            value={expertID ?? ""}
            options={[
              { value: "", label: t("projects.tasks.noExpert") },
              ...experts.map((expert) => ({
                value: expert.expert_id,
                label: taskExpertLabel(expert, t),
              })),
            ]}
            onChange={(id) => setExpertID(id || null)}
          />
        </Space>
      </Modal>
    </>
  );
}
