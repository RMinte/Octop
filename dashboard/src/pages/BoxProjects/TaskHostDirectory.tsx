import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  bridgeApi,
  type BridgeConnection,
  type BridgeTaskCapabilities,
} from "../../api/modules/bridge";
import {
  createProjectTasksApi,
  projectTasksApi,
  type TaskSummary,
} from "../../api/modules/projects";
import {
  conversationUrl,
  taskRefKey,
  type TaskRef,
} from "../../api/taskTransport";
interface DirectoryRow extends TaskSummary {
  ref: TaskRef;
  hostName: string;
  cached?: boolean;
}
const supported = (value?: BridgeTaskCapabilities) =>
  value?.available &&
  value.protocol_version === 3 &&
  value.capabilities.tasks &&
  value.capabilities.projects &&
  value.capabilities.task_ws;
export default function TaskHostDirectory({
  hostID,
}: {
  hostID: string | null;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [hosts, setHosts] = useState<BridgeConnection[]>([]);
  const [capabilities, setCapabilities] = useState<
    Record<string, BridgeTaskCapabilities>
  >({});
  const [rows, setRows] = useState<DirectoryRow[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [version, setVersion] = useState(0);
  const local = t("projects.tasks.hosts.local");
  useEffect(() => {
    let active = true;
    void bridgeApi
      .list()
      .then(async (connections) => {
        if (!active) return;
        setHosts(connections);
        const results = await Promise.allSettled(
          connections.map(
            async (host) =>
              [
                host.connection_id,
                await bridgeApi.taskCapabilities(host.connection_id),
              ] as const,
          ),
        );
        if (active)
          setCapabilities(
            Object.fromEntries(
              results.flatMap((value) =>
                value.status === "fulfilled" ? [value.value] : [],
              ),
            ),
          );
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [version]);
  const load = async () => {
    setLoading(true);
    setErrors([]);
    const scopes = [
      { id: null as string | null, name: local },
      ...hosts.map((host) => ({
        id: host.connection_id,
        name: host.display_name,
      })),
    ];
    const results = await Promise.allSettled(
      scopes.map(async (host) => {
        try {
          if (host.id && !supported(capabilities[host.id]))
            throw new Error("offline");
          const page = await (host.id
            ? createProjectTasksApi(host.id)
            : projectTasksApi
          ).list(null, null, false, { scope: "all" });
          return {
            rows: page.items.map((task) => ({
              ...task,
              ref: { host_id: host.id, task_id: task.task_id },
              hostName: host.name,
            })),
            cached: false,
          };
        } catch (error) {
          if (!host.id) throw error;
          const cache = await bridgeApi.taskReferences(host.id);
          return {
            rows: cache.items.map((item) => ({
              ...item.summary,
              ref: { host_id: host.id, task_id: item.task_id },
              hostName: host.name,
              cached: true,
            })),
            cached: true,
          };
        }
      }),
    );
    setRows(
      results
        .flatMap((value) =>
          value.status === "fulfilled" ? value.value.rows : [],
        )
        .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)),
    );
    setErrors(
      results.flatMap((value, index) =>
        value.status === "rejected" || value.value.cached
          ? [scopes[index].name]
          : [],
      ),
    );
    setLoading(false);
  };
  return (
    <Space direction="vertical" style={{ width: "100%", marginBottom: 12 }}>
      <Space wrap>
        <Typography.Text>{t("projects.tasks.hosts.host")}</Typography.Text>
        <Select
          aria-label={t("projects.tasks.hosts.host")}
          value={hostID ?? "local"}
          style={{ minWidth: 220 }}
          options={[
            { value: "local", label: local },
            ...hosts.map((host) => ({
              value: host.connection_id,
              label: `${host.display_name}${
                supported(capabilities[host.connection_id])
                  ? ""
                  : ` · ${t("projects.tasks.hosts.unavailable")}`
              }`,
              disabled: !supported(capabilities[host.connection_id]),
            })),
          ]}
          onChange={(value) =>
            navigate(conversationUrl(null, value === "local" ? null : value))
          }
        />
        <Button size="small" onClick={() => setVersion((v) => v + 1)}>
          {t("projects.refresh")}
        </Button>
      </Space>
      <Collapse
        size="small"
        onChange={(keys) => {
          if (keys.length) void load();
        }}
        items={[
          {
            key: "all",
            label: t("projects.tasks.hosts.all"),
            children: (
              <Space direction="vertical" style={{ width: "100%" }}>
                <Typography.Text type="secondary">
                  {t("projects.tasks.hosts.recentHint")}
                </Typography.Text>
                {loading ? (
                  <Spin />
                ) : (
                  rows.map((task) => (
                    <Button
                      key={taskRefKey(task.ref)}
                      type="text"
                      block
                      style={{
                        height: "auto",
                        textAlign: "left",
                        justifyContent: "flex-start",
                      }}
                      onClick={() => navigate(conversationUrl(task.ref))}
                    >
                      <Tag>{task.hostName}</Tag>
                      {task.cached && (
                        <Tag>{t("projects.tasks.hosts.cached")}</Tag>
                      )}
                      {task.title}
                    </Button>
                  ))
                )}
                {errors.length > 0 && (
                  <Alert
                    type="warning"
                    message={t("projects.tasks.hosts.readFailed", {
                      hosts: errors.join(", "),
                    })}
                  />
                )}
              </Space>
            ),
          },
        ]}
      />
    </Space>
  );
}
