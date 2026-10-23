import { Button, Space, Tag, Typography } from "antd";
import { FolderOpen, Settings2, SquarePen } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { Project } from "../../api/modules/projects";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import styles from "./Workspace.module.less";

export default function ProjectOverview({
  project,
  onEdit,
  onManage,
  onNew,
}: {
  project: Project;
  onEdit: () => void;
  onManage: () => void;
  onNew: () => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const groups = [
    [
      t("projects.skills"),
      project.skills.map((item) => ("source" in item ? item.name : item.slug)),
    ],
    [t("projects.mcp"), project.mcp_servers],
    [t("projects.plugins"), project.plugins],
    [t("projects.subagents"), project.subagents],
  ] as const;
  return (
    <section
      className={styles.overview}
      aria-label={t("projects.workspace.overview")}
    >
      <header className={styles.overviewHeading}>
        <FolderOpen size={20} />
        <div>
          <h2>{project.name}</h2>
          <span>{t("projects.workspace.projectSummary")}</span>
        </div>
        {project.archived && <Tag>{t("projects.archivedList")}</Tag>}
      </header>
      {project.description && <p>{project.description}</p>}
      {project.archived && (
        <p className={styles.secondary}>{t("projects.archivedHint")}</p>
      )}
      <Space wrap className={styles.overviewActions}>
        <Button
          type="primary"
          icon={<SquarePen size={15} />}
          disabled={project.archived}
          onClick={onNew}
        >
          {t("projects.workspace.newInProject")}
        </Button>
        <Button icon={<Settings2 size={15} />} onClick={onEdit}>
          {t("projects.edit")}
        </Button>
        <Button type="text" onClick={onManage}>
          {t("projects.workspace.manageProject")}
        </Button>
      </Space>
      <dl className={styles.projectFacts}>
        <dt>{t("projects.directory")}</dt>
        <dd>
          <Typography.Text copyable={!!project.directory}>
            {project.directory || t("projects.noDirectory")}
          </Typography.Text>
        </dd>
        <dt>{t("projects.clientDirectory")}</dt>
        <dd>
          <Typography.Text copyable={!!project.client_directory}>
            {project.client_directory || t("projects.noClientDirectory")}
          </Typography.Text>
        </dd>
        {groups.map(([label, values]) => (
          <div key={label} className={styles.factRow}>
            <dt>{label}</dt>
            <dd>{values.length ? values.join(" · ") : t("projects.none")}</dd>
          </div>
        ))}
        <dt>{t("projects.collaboration")}</dt>
        <dd>
          {[
            project.memory_enabled && t("projects.memory"),
            project.team_enabled && t("projects.team"),
          ]
            .filter(Boolean)
            .join(" · ") || t("projects.none")}
        </dd>
        <dt>{t("projects.instructions")}</dt>
        <dd className={styles.instructions}>
          {project.instructions || t("projects.noInstructions")}
        </dd>
      </dl>
      <div className={styles.secondary}>
        {t("projects.updated", {
          time: formatServerIsoDateTime(project.updated_at, timeZone),
        })}
      </div>
    </section>
  );
}
