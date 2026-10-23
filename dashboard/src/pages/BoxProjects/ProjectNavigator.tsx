import type { ReactNode } from "react";
import { useState } from "react";
import {
  Archive,
  ChevronDown,
  CircleDot,
  Folder,
  MessagesSquare,
  Pin,
  Plus,
  Settings2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type { Project } from "../../api/modules/projects";
import styles from "./Workspace.module.less";

export type WorkspaceScope =
  | "all"
  | "active"
  | "pinned"
  | "standalone"
  | "project";

export default function ProjectNavigator({
  projects,
  scope,
  projectID,
  onScope,
  onProject,
  onEdit,
  onCreate,
  more,
}: {
  projects: Project[];
  scope: WorkspaceScope;
  projectID?: string | null;
  onScope: (scope: WorkspaceScope) => void;
  onProject: (project: Project) => void;
  onEdit: (project: Project) => void;
  onCreate: () => void;
  more?: ReactNode;
}) {
  const { t } = useTranslation();
  const [showArchived, setShowArchived] = useState(false);
  const entries = projects.filter(
    (project) => !!project.archived === showArchived,
  );
  return (
    <nav
      className={styles.navigator}
      aria-label={t("projects.workspace.navigation")}
    >
      {(
        [
          ["all", MessagesSquare, "all"],
          ["active", CircleDot, "active"],
          ["pinned", Pin, "pinned"],
        ] as const
      ).map(([value, Icon, label]) => (
        <button
          key={value}
          type="button"
          className={scope === value ? styles.navSelected : styles.navButton}
          aria-current={scope === value ? "page" : undefined}
          onClick={() => onScope(value)}
        >
          <Icon size={15} />
          <span>{t(`projects.workspace.${label}`)}</span>
        </button>
      ))}
      <div className={styles.navigatorHeading}>
        <span>{t("projects.list")}</span>
        <button
          type="button"
          className={styles.iconButton}
          aria-label={t("projects.create")}
          onClick={onCreate}
        >
          <Plus size={15} />
        </button>
      </div>
      <div className={styles.projectRows}>
        {entries.map((project) => (
          <div className={styles.projectRow} key={project.project_id}>
            <button
              type="button"
              className={
                scope === "project" && projectID === project.project_id
                  ? styles.navSelected
                  : styles.navButton
              }
              aria-current={
                scope === "project" && projectID === project.project_id
                  ? "page"
                  : undefined
              }
              onClick={() => onProject(project)}
            >
              <Folder size={15} />
              <span>{project.name}</span>
            </button>
            <button
              type="button"
              className={styles.iconButton}
              aria-label={t("projects.workspace.editProject", {
                name: project.name,
              })}
              onClick={() => onEdit(project)}
            >
              <Settings2 size={14} />
            </button>
          </div>
        ))}
        {!entries.length && (
          <span className={styles.emptyProjects}>{t("projects.empty")}</span>
        )}
      </div>
      <button
        type="button"
        className={
          scope === "standalone" ? styles.navSelected : styles.navButton
        }
        aria-current={scope === "standalone" ? "page" : undefined}
        onClick={() => onScope("standalone")}
      >
        <MessagesSquare size={15} />
        <span>{t("projects.workspace.standalone")}</span>
      </button>
      <button
        type="button"
        className={styles.navButton}
        aria-pressed={showArchived}
        onClick={() => setShowArchived((value) => !value)}
      >
        <Archive size={14} />
        <span>
          {t(showArchived ? "projects.activeList" : "projects.archivedList")}
        </span>
        <ChevronDown size={12} />
      </button>
      {more && <div className={styles.navigatorMore}>{more}</div>}
    </nav>
  );
}
