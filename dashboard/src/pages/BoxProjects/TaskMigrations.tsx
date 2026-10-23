import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Drawer,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  taskMigrationsApi,
  type MigrationBatch,
  type MigrationPreflight,
  type MigrationRequest,
} from "../../api/modules/taskMigrations";
import type { Project } from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";

export default function TaskMigrations({ projects }: { projects: Project[] }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [automatic, setAutomatic] = useState(false);
  const [batches, setBatches] = useState<MigrationBatch[]>([]);
  const [preview, setPreview] = useState<MigrationPreflight>();
  const [projectID, setProjectID] = useState<string | null>(null);
  const pending = useRef<MigrationRequest>();
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!open) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      setLoading(true);
      try {
        const [rows, setting] = await Promise.all([
          taskMigrationsApi.list(),
          taskMigrationsApi.automatic(),
        ]);
        if (active) {
          setBatches(rows);
          setAutomatic(setting.enabled);
          if (
            rows.some((row) =>
              ["queued", "running", "waiting"].includes(row.status),
            )
          )
            timer = setTimeout(() => void refresh(), 2500);
        }
      } catch (error) {
        if (active)
          setError(apiErrorMessage(error, t("projects.tasks.failure"), t));
      } finally {
        if (active) setLoading(false);
      }
    };
    void refresh();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [open, version, t]);
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (error) {
      setError(apiErrorMessage(error, t("projects.tasks.failure"), t));
    } finally {
      setBusy(false);
    }
  };
  const prepare = () =>
    run(async () => {
      pending.current = {
        request_id: crypto.randomUUID(),
        thread_ids: [],
        project_id: projectID,
      };
      setPreview(await taskMigrationsApi.preflight(pending.current));
    });
  const start = () =>
    run(async () => {
      if (!pending.current) return;
      await taskMigrationsApi.create(pending.current);
      setPreview(undefined);
      setVersion((v) => v + 1);
    });
  return (
    <>
      <Button onClick={() => setOpen(true)}>
        {t("projects.tasks.migration.title")}
      </Button>
      <Drawer
        width={760}
        open={open}
        onClose={() => setOpen(false)}
        title={t("projects.tasks.migration.title")}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Alert type="info" message={t("projects.tasks.migration.hint")} />
          {error && <Alert type="error" message={error} />}
          <Space>
            <Switch
              aria-label={t("projects.tasks.migration.automatic")}
              checked={automatic}
              disabled={busy}
              onChange={(enabled) =>
                void run(async () => {
                  const setting = await taskMigrationsApi.setAutomatic(enabled);
                  setAutomatic(setting.enabled);
                  setVersion((v) => v + 1);
                })
              }
            />
            <Typography.Text>
              {t("projects.tasks.migration.automatic")}
            </Typography.Text>
          </Space>
          <Select
            aria-label={t("projects.tasks.branchProject")}
            value={projectID ?? ""}
            style={{ width: "100%" }}
            disabled={busy}
            options={[
              { value: "", label: t("projects.tasks.noProject") },
              ...projects
                .filter((p) => !p.archived)
                .map((p) => ({ value: p.project_id, label: p.name })),
            ]}
            onChange={(id) => {
              setProjectID(id || null);
              setPreview(undefined);
              pending.current = undefined;
            }}
          />
          <Button disabled={busy} onClick={() => void prepare()}>
            {t("projects.tasks.migration.preflight")}
          </Button>
          {preview && (
            <>
              <Alert
                type="info"
                message={t("projects.tasks.migration.preview", {
                  total: preview.total,
                  eligible: preview.eligible,
                })}
              />
              <Collapse
                items={[
                  {
                    key: "sources",
                    label: t("projects.tasks.migration.sources"),
                    children: preview.candidates.map((row) => (
                      <Typography.Paragraph key={row.thread_id}>
                        <Tag>{row.channel_type}</Tag>
                        {row.thread_id} · {row.disposition}
                      </Typography.Paragraph>
                    )),
                  },
                ]}
              />
              <Button
                type="primary"
                loading={busy}
                disabled={!preview.eligible}
                onClick={() => void start()}
              >
                {t("projects.tasks.migration.start")}
              </Button>
            </>
          )}
          {loading && !batches.length ? (
            <Spin />
          ) : (
            batches.map((batch) => (
              <Collapse
                key={batch.batch_id}
                items={[
                  {
                    key: batch.batch_id,
                    label: (
                      <Space>
                        <Tag>
                          {t(`projects.tasks.migration.status.${batch.status}`)}
                        </Tag>
                        {
                          batch.items.filter(
                            (item) => item.status === "completed",
                          ).length
                        }
                        /{batch.items.length}
                        <Button
                          size="small"
                          disabled={busy || batch.status === "completed"}
                          onClick={(event) => {
                            event.stopPropagation();
                            void run(async () => {
                              await taskMigrationsApi.control(
                                batch.batch_id,
                                batch.status === "paused" ? "resume" : "pause",
                              );
                              setVersion((v) => v + 1);
                            });
                          }}
                        >
                          {t(
                            batch.status === "paused"
                              ? "projects.tasks.migration.resume"
                              : "projects.tasks.migration.pause",
                          )}
                        </Button>
                      </Space>
                    ),
                    children: batch.items.map((item) => (
                      <Space
                        key={item.item_id}
                        direction="vertical"
                        style={{ width: "100%", marginBottom: 14 }}
                      >
                        <Space>
                          <Tag>
                            {t(
                              `projects.tasks.migration.status.${item.status}`,
                            )}
                          </Tag>
                          <Typography.Text>
                            {item.source_thread_id}
                          </Typography.Text>
                          {item.status === "completed" && (
                            <Button
                              size="small"
                              onClick={() =>
                                navigate(
                                  `/conversations/${encodeURIComponent(
                                    item.target_task_id,
                                  )}`,
                                )
                              }
                            >
                              {t("projects.tasks.migration.open")}
                            </Button>
                          )}
                        </Space>
                        {item.error_code && (
                          <Typography.Text type="warning">
                            {item.error_code}
                          </Typography.Text>
                        )}
                        {Number(item.manifest.missing_attachment_count ?? 0) >
                          0 && (
                          <Alert
                            type="warning"
                            message={t("projects.tasks.migration.missing", {
                              count: item.manifest.missing_attachment_count,
                            })}
                          />
                        )}
                      </Space>
                    )),
                  },
                ]}
              />
            ))
          )}
        </Space>
      </Drawer>
    </>
  );
}
