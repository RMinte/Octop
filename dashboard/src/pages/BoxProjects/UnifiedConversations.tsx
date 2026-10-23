import TaskMigrations from "./TaskMigrations";
import { useTaskHost } from "../../context/TaskHostContext";
import TaskHostBoundary from "./TaskHostBoundary";
import TaskHostDirectory from "./TaskHostDirectory";
import { useEffect, useState } from "react";
import { Alert, Button, Select, Space, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { type Project } from "../../api/modules/projects";
import PageShell from "../../layouts/PageShell";
import { apiErrorMessage } from "../../utils/apiError";
import TaskConversations from "./TaskConversations";
import { useTaskMessageQueue } from "./useTaskMessageQueue";

/** Task IDs remain stable when the selected expert changes. Legacy chats stay at /chat. */
export default function UnifiedConversations() {
  const [params] = useSearchParams();
  const hostID = params.get("host_id");
  return (
    <TaskHostBoundary key={hostID ?? "local"} hostID={hostID}>
      <UnifiedConversationBody />
    </TaskHostBoundary>
  );
}
function UnifiedConversationBody() {
  const { projectsApi, projectTasksApi, hostID } = useTaskHost();
  const { t } = useTranslation();
  const { taskID } = useParams<{ taskID: string }>();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const queue = useTaskMessageQueue();
  const [projects, setProjects] = useState<Project[]>([]);
  const [resolved, setResolved] = useState<{
    taskID: string | undefined;
    projectID: string | null;
    project?: Project;
  } | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  const projectParam = params.get("project_id");
  useEffect(() => {
    let disposed = false;
    void (async () => {
      try {
        const rows = await projectsApi.list();
        const projectID = taskID
          ? (await projectTasksApi.get(taskID)).project_id
          : projectParam;
        const project = projectID
          ? rows.find((row) => row.project_id === projectID) ??
            (await projectsApi.get(projectID))
          : undefined;
        if (!disposed) {
          setProjects(rows);
          setResolved({ taskID, projectID, project });
          setError("");
        }
      } catch (err) {
        if (!disposed)
          setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
      }
    })();
    return () => {
      disposed = true;
    };
  }, [taskID, projectParam, version, t, projectsApi, projectTasksApi]);
  const selectTask = (id: string | null) => {
    const next = new URLSearchParams(params);
    next.delete("expert_id");
    next.delete("project_id");
    if (!id && resolved?.projectID) next.set("project_id", resolved.projectID);
    navigate(
      `${id ? `/conversations/${encodeURIComponent(id)}` : "/conversations"}${
        next.size ? `?${next}` : ""
      }`,
    );
  };
  const ready =
    resolved?.taskID === taskID &&
    (taskID || resolved?.projectID === projectParam);
  return (
    <PageShell
      title={t("projects.title")}
      subtitle={t("projects.tasks.unifiedHint")}
    >
      <TaskHostDirectory hostID={hostID} />
      {hostID && (
        <Alert
          style={{ marginBottom: 12 }}
          type="info"
          message={t("projects.tasks.hosts.remoteHint", { host: hostID })}
        />
      )}
      <Space wrap style={{ marginBottom: 12 }}>
        <Button
          onClick={() =>
            navigate(
              `/box-projects${
                hostID ? `?host_id=${encodeURIComponent(hostID)}` : ""
              }`,
            )
          }
        >
          {t("projects.list")}
        </Button>
        {!hostID && <TaskMigrations projects={projects} />}
        <Button onClick={() => navigate("/chat")}>
          {t("projects.tasks.legacyHistory")}
        </Button>
        <Typography.Text>{t("projects.tasks.project")}</Typography.Text>
        <Select
          aria-label={t("projects.tasks.project")}
          style={{ minWidth: 220 }}
          disabled={!!taskID}
          value={resolved?.projectID ?? ""}
          options={[
            { value: "", label: t("projects.tasks.noProject") },
            ...projects
              .filter(
                (p) => !p.archived || p.project_id === resolved?.projectID,
              )
              .map((p) => ({
                value: p.project_id,
                label: p.name,
                disabled: p.archived,
              })),
          ]}
          onChange={(id) => {
            const next = new URLSearchParams(params);
            if (id) next.set("project_id", id);
            else next.delete("project_id");
            navigate(`/conversations${next.size ? `?${next}` : ""}`);
          }}
        />
        {taskID && (
          <Typography.Text type="secondary">
            {t("projects.tasks.projectFixed")}
          </Typography.Text>
        )}
      </Space>
      {error ? (
        <Alert
          type="error"
          message={error}
          action={
            <Button onClick={() => setVersion((v) => v + 1)}>
              {t("projects.refresh")}
            </Button>
          }
        />
      ) : !ready || !resolved ? (
        <Spin />
      ) : (
        <TaskConversations
          projectID={resolved.projectID}
          project={resolved.project}
          projectOptions={projects}
          queue={queue}
          selectedTaskID={taskID ?? null}
          onSelectTask={selectTask}
          allTasks
          defaultExpertID={params.get("expert_id")}
        />
      )}
    </PageShell>
  );
}
