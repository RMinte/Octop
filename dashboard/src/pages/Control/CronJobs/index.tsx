import { useState, useEffect, useMemo } from "react";
import { Button, Form, Segmented, Spin, Tooltip, Select } from "antd";
import { LayoutGrid, List, RefreshCw } from "lucide-react";
import type { CronJobSpecOutput } from "../../../api/types";
import { useTranslation } from "react-i18next";
import {
  createColumns,
  CronJobCard,
  ExecuteNowModal,
  JobDrawer,
  JobDetailDrawer,
  useCronJobs,
} from "./components";
import type { CronJobFormValues } from "./useCronJobs";
import { useCardTableView } from "../../../hooks/useCardTableView";
import { showConfirmModal } from "../../../utils/confirmModal";
import { OctopEmptyMascot } from "../../../components/EmptyState";
import { ResizableTable } from "../../../components/ResizableTable";
import PageShell from "../../../layouts/PageShell";
import { taskExampleColumns } from "./taskExamples";
import {
  projectsApi,
  projectTasksApi,
  type Project,
  type TaskResources,
} from "../../../api/modules/projects";
import { buildDefaultFormValues } from "./components/constants";
import styles from "./index.module.less";

type CronJob = CronJobSpecOutput;

interface CronJobsEmptyStateProps {
  onCreate: () => void;
  onSuggestionClick: (text: string) => void;
  suggestions: string[];
}

function CronJobsEmptyState({
  onCreate,
  onSuggestionClick,
  suggestions,
}: CronJobsEmptyStateProps) {
  const { t } = useTranslation();

  return (
    <div className={styles.emptyState}>
      <div className={styles.emptyStateIcon}>
        <OctopEmptyMascot />
      </div>
      <h2 className={styles.emptyStateTitle}>{t("cronJobs.noJobs")}</h2>
      <p className={styles.emptyStateDesc}>{t("cronJobs.noJobsDesc")}</p>
      {suggestions.length > 0 ? (
        <div
          className={`${styles.emptyStateSuggestions} ${
            taskExampleColumns(suggestions.length) === 2
              ? styles.emptyStateSuggestionsCols2
              : ""
          }`}
        >
          {suggestions.map((text, i) => (
            <button
              key={i}
              type="button"
              className={styles.emptyStateSuggestionItem}
              title={text}
              onClick={() => onSuggestionClick(text)}
            >
              <span className={styles.emptyStateSuggestionText}>{text}</span>
              <span className={styles.emptyStateSuggestionArrow}>→</span>
            </button>
          ))}
        </div>
      ) : null}
      <Button
        type="primary"
        onClick={onCreate}
        className={styles.emptyStateCreateBtn}
      >
        + {t("cronJobs.createJob")}
      </Button>
    </div>
  );
}

function CronJobsPage() {
  const { t } = useTranslation();
  const { isMobile, viewMode, setViewMode, showCardView } =
    useCardTableView("table");
  const [experts, setExperts] = useState<NonNullable<TaskResources["experts"]>>(
    [],
  );
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectFilter, setProjectFilter] = useState<string>("all");
  const taskExamples = [
    t("cronJobs.noJobsSuggestion1"),
    t("cronJobs.noJobsSuggestion2"),
    t("cronJobs.noJobsSuggestion3"),
  ];
  useEffect(() => {
    let cancelled = false;
    void projectTasksApi
      .resources()
      .then((resources) => {
        if (!cancelled) setExperts(resources.experts ?? []);
      })
      .catch(console.error);
    void projectsApi
      .list()
      .then((rows) => {
        if (!cancelled) setProjects(rows);
      })
      .catch(console.error);
    return () => {
      cancelled = true;
    };
  }, []);
  const {
    jobs: allJobs,
    loading,
    listRefreshing,
    listStale,
    cronTimezone,
    createJob,
    updateJob,
    deleteJob,
    toggleEnabled,
    executeNow,
    jobToFormValues,
    refetchJobs,
  } = useCronJobs();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingJob, setEditingJob] = useState<CronJobFormValues | null>(null);
  const [form] = Form.useForm<CronJobFormValues>();
  const [refreshing, setRefreshing] = useState(false);

  // Detail drawer state
  const [detailDrawerOpen, setDetailDrawerOpen] = useState(false);
  const [detailJob, setDetailJob] = useState<CronJob | null>(null);

  // Execute-now confirmation
  const [executingJob, setExecutingJob] = useState<CronJob | null>(null);
  const [executing, setExecuting] = useState(false);

  const jobs = useMemo(
    () =>
      allJobs
        .filter(
          (job) =>
            projectFilter === "all" ||
            (projectFilter === "none"
              ? !job.project_id
              : job.project_id === projectFilter),
        )
        .map((job) => ({
          ...job,
          project_name:
            projects.find((project) => project.project_id === job.project_id)
              ?.name ??
            job.project_id ??
            t("cronJobs.noProject"),
          expert_name:
            experts.find(
              (expert) => expert.expert_id === job.selected_expert_id,
            )?.name ??
            job.selected_expert_id ??
            t("cronJobs.generalAssistant"),
        })),
    [allJobs, projectFilter, projects, experts, t],
  );
  const defaultProjectId =
    projectFilter !== "all" && projectFilter !== "none"
      ? projects.find(
          (project) =>
            project.project_id === projectFilter && !project.archived,
        )?.project_id ?? null
      : null;

  const handleDetail = (job: CronJob) => {
    setDetailJob(job);
    setDetailDrawerOpen(true);
  };

  const handleDetailClose = () => {
    setDetailDrawerOpen(false);
    setDetailJob(null);
  };

  // Keep detail drawer in sync after list refresh (e.g. run-now).
  useEffect(() => {
    if (!detailDrawerOpen || !detailJob) return;
    const fresh = jobs.find((j) => j.id === detailJob.id);
    if (fresh) setDetailJob(fresh);
  }, [jobs, detailDrawerOpen, detailJob]);

  const handleCreate = () => {
    setEditingJob(null);
    form.resetFields();
    setDrawerOpen(true);
  };

  const handleSuggestionClick = (text: string) => {
    setEditingJob({
      ...buildDefaultFormValues(cronTimezone),
      prompt: text,
      project_id: defaultProjectId,
    });
    setDrawerOpen(true);
  };

  const handleEdit = (job: CronJob) => {
    setEditingJob(jobToFormValues(job, cronTimezone));
    setDrawerOpen(true);
  };

  const handleDelete = (jobId: string) => {
    showConfirmModal(
      {
        title: t("cronJobs.confirmDelete"),
        content: t("cronJobs.deleteConfirm"),
        okText: t("common.delete"),
        okType: "primary",
        cancelText: t("common.cancel"),
        onOk: async () => {
          await deleteJob(jobId);
        },
      },
      { isMobile },
    );
  };

  const handleToggleEnabled = async (job: CronJob) => {
    await toggleEnabled(job);
  };

  const handleExecuteNow = async (job: CronJob) => {
    setExecutingJob(job);
  };

  const handleExecuteNowConfirm = async () => {
    if (!executingJob) return;
    setExecuting(true);
    try {
      await executeNow(executingJob.id);
      setExecutingJob(null);
    } finally {
      setExecuting(false);
    }
  };

  const handleExecuteNowCancel = () => {
    if (executing) return;
    setExecutingJob(null);
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await refetchJobs(true);
    } finally {
      setRefreshing(false);
    }
  };

  const handleDrawerClose = () => {
    setDrawerOpen(false);
    setEditingJob(null);
  };

  const handleSubmit = async (values: CronJobFormValues) => {
    let success = false;
    if (editingJob?.id) {
      success = await updateJob(editingJob.id, values);
    } else {
      success = await createJob(values);
    }
    if (success) {
      setDrawerOpen(false);
      setEditingJob(null);
    }
  };

  const columns = createColumns({
    onDetail: handleDetail,
    onToggleEnabled: handleToggleEnabled,
    onExecuteNow: handleExecuteNow,
    onEdit: handleEdit,
    onDelete: handleDelete,
    t,
    timeZone: cronTimezone,
    stickyColumns: !isMobile,
  });

  const contentBusy = listRefreshing || refreshing || listStale;
  const showList = jobs.length > 0;
  const showEmpty = !loading && !listStale && jobs.length === 0;
  const showBodySpinner = loading || (contentBusy && !showList && !showEmpty);
  const showToolbar = true;

  return (
    <PageShell
      title={t("pageShell.tasks.title")}
      subtitle={t("pageShell.tasks.subtitle")}
    >
      <Select
        value={projectFilter}
        onChange={setProjectFilter}
        style={{ width: 280, maxWidth: "100%", marginBottom: 16 }}
        options={[
          { value: "all", label: t("cronJobs.allProjects") },
          { value: "none", label: t("cronJobs.noProject") },
          ...projects.map((project) => ({
            value: project.project_id,
            label: project.name,
          })),
        ]}
      />
      {showToolbar ? (
        <div className={styles.gridToolbar}>
          <span className={styles.gridCount}>
            {t("cronJobs.totalItems", { count: jobs.length })}
            {contentBusy ? (
              <span className={styles.refreshHint}>
                {" · "}
                {listStale ? t("cronJobs.loadingJobs") : t("cronJobs.syncing")}
              </span>
            ) : null}
          </span>
          <div className={styles.gridToolbarRight}>
            <Segmented
              size="small"
              value={viewMode}
              onChange={(v) => setViewMode(v as "table" | "card")}
              options={[
                {
                  value: "table",
                  label: (
                    <span className={styles.viewModeLabel}>
                      <List size={14} />
                      {t("cronJobs.viewTable")}
                    </span>
                  ),
                },
                {
                  value: "card",
                  label: (
                    <span className={styles.viewModeLabel}>
                      <LayoutGrid size={14} />
                      {t("cronJobs.viewCard")}
                    </span>
                  ),
                },
              ]}
            />
            <Tooltip title={t("common.refresh")}>
              <Button
                icon={<RefreshCw size={15} />}
                loading={refreshing || listRefreshing || listStale}
                onClick={() => void handleRefresh()}
              />
            </Tooltip>
            <Button type="primary" onClick={handleCreate} disabled={listStale}>
              + {t("cronJobs.createJob")}
            </Button>
          </div>
        </div>
      ) : null}

      {showBodySpinner ? (
        <div className={styles.firstLoad}>
          <Spin />
        </div>
      ) : showEmpty ? (
        <CronJobsEmptyState
          onCreate={handleCreate}
          onSuggestionClick={handleSuggestionClick}
          suggestions={taskExamples}
        />
      ) : (
        <div
          className={`${styles.listShell} ${
            contentBusy ? styles.listShellBusy : ""
          }`}
        >
          {contentBusy ? (
            <div className={styles.listBusyBar} aria-hidden>
              <Spin size="small" />
            </div>
          ) : null}
          {showCardView ? (
            <div className={styles.cardGrid}>
              {jobs.map((job) => (
                <CronJobCard
                  key={job.id}
                  job={job}
                  timeZone={cronTimezone}
                  onToggleEnabled={handleToggleEnabled}
                  onExecuteNow={handleExecuteNow}
                  onEdit={handleEdit}
                  onDelete={handleDelete}
                />
              ))}
            </div>
          ) : (
            <ResizableTable
              className={styles.cronTable}
              columns={columns}
              dataSource={jobs}
              rowKey="id"
              size="middle"
              tableLayout="fixed"
              scroll={{ x: 1562 }}
              storageKey="cron-jobs-table-widths"
              minWidth={72}
              pagination={{
                pageSize: 10,
                showSizeChanger: false,
                showTotal: (total) =>
                  t("cronJobs.totalItems", { count: total }),
              }}
            />
          )}
        </div>
      )}

      <JobDrawer
        open={drawerOpen}
        editingJob={editingJob}
        projects={projects}
        experts={experts}
        defaultProjectId={defaultProjectId}
        cronTimezone={cronTimezone}
        form={form}
        onClose={handleDrawerClose}
        onSubmit={handleSubmit}
      />

      <JobDetailDrawer
        open={detailDrawerOpen}
        job={detailJob}
        timeZone={cronTimezone}
        onClose={handleDetailClose}
      />

      <ExecuteNowModal
        open={executingJob !== null}
        job={executingJob}
        loading={executing}
        onCancel={handleExecuteNowCancel}
        onConfirm={handleExecuteNowConfirm}
      />
    </PageShell>
  );
}

export default CronJobsPage;
