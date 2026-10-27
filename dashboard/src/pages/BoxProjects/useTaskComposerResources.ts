import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { useTaskHost } from "../../context/TaskHostContext";
import type {
  Project,
  ProjectSkill,
  TaskExecutionOptions,
  TaskResources,
} from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";
import { projectSkillKey } from "./ProjectSkillsField";

type Query = {
  kind: "file" | "skill";
  start: number;
  end: number;
  text: string;
  quoted?: boolean;
};
export type ComposerResourceItem =
  | {
      kind: "file";
      key: string;
      label: string;
      path: string;
      directory: boolean;
    }
  | {
      kind: "skill";
      key: string;
      label: string;
      description: string;
      skill: ProjectSkill;
      conflict: boolean;
    };

const HIDDEN = new Set([
  ".octop",
  ".octop-auth",
  ".git",
  ".venv",
  "node_modules",
  "__pycache__",
  "_builtin_skills",
]);

export function composerResourceQuery(
  text: string,
  cursor: number,
): Query | null {
  const before = text.slice(0, cursor);
  const file = /(?:^|\s)@("[^"\n]*|[^\s@]*)$/.exec(before);
  if (file) {
    const raw = file[1];
    return {
      kind: "file",
      start: before.length - raw.length - 1,
      end: cursor,
      text: raw.replace(/^"/, ""),
      quoted: raw.startsWith('"'),
    };
  }
  const skill = /(?:^|\s)\/([\p{L}\p{N}_.-]*)$/u.exec(before);
  return skill
    ? {
        kind: "skill",
        start: before.length - skill[1].length - 1,
        end: cursor,
        text: skill[1],
      }
    : null;
}

export function inheritedComposerSkills(
  project: Project | undefined,
  resources: TaskResources | null,
  expertID: string | null,
): ProjectSkill[] {
  return [
    ...new Map(
      [
        ...(resources?.experts?.find((expert) => expert.expert_id === expertID)
          ?.default_skills ?? []),
        ...(project?.skills ?? []),
      ].map((skill) => [projectSkillKey(skill), skill]),
    ).values(),
  ];
}

export interface TaskComposerResourcesProps {
  taskID: string | null;
  project?: Project;
  selectedExpertID: string | null;
  text: string;
  onTextChange: (text: string) => void;
  value: TaskExecutionOptions;
  onChange: (value: TaskExecutionOptions) => void;
  resources: TaskResources | null;
  disabled: boolean;
  skillsEnabled?: boolean;
  getInputElement: () => HTMLTextAreaElement | null;
}

export function useTaskComposerResources({
  taskID,
  project,
  selectedExpertID,
  text,
  onTextChange,
  value,
  onChange,
  resources,
  disabled,
  skillsEnabled = true,
  getInputElement,
}: TaskComposerResourcesProps) {
  const { taskWorkbenchApi, projectsApi, hostID } = useTaskHost();
  const { t } = useTranslation();
  const [query, setQuery] = useState<Query | null>(null);
  const [index, setIndex] = useState(0);
  const [files, setFiles] = useState<ComposerResourceItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [pendingSkill, setPendingSkill] = useState<Extract<
    ComposerResourceItem,
    { kind: "skill" }
  > | null>(null);
  const dismissed = useRef("");
  const canReferenceFiles = !!taskID || !!project?.directory;
  const projectID = project?.project_id;
  const projectRevision = project?.revision;
  const inherited = useMemo(
    () => inheritedComposerSkills(project, resources, selectedExpertID),
    [project, resources, selectedExpertID],
  );
  const effective = value.skills ?? inherited;
  const expert = resources?.experts?.find(
    (entry) => entry.expert_id === selectedExpertID,
  );
  const skillsReady =
    skillsEnabled &&
    resources !== null &&
    (value.skills != null ||
      !selectedExpertID ||
      expert?.default_skills !== undefined);

  const onCursorChange = useCallback(() => {
    const cursor = getInputElement()?.selectionStart ?? text.length;
    const next = disabled ? null : composerResourceQuery(text, cursor);
    const key = JSON.stringify([text, cursor]);
    setQuery(
      key === dismissed.current || (next?.kind === "skill" && !skillsEnabled)
        ? null
        : next,
    );
    setIndex(0);
  }, [disabled, getInputElement, text, skillsEnabled]);
  useEffect(onCursorChange, [onCursorChange]);
  useEffect(() => {
    setPendingSkill(null);
    dismissed.current = "";
  }, [taskID, project?.project_id, hostID, selectedExpertID]);

  useEffect(() => {
    setFiles([]);
    setError("");
    setLoading(false);
    if (query?.kind !== "file" || disabled || !canReferenceFiles) return;
    const raw = query.text.replace(/\\/g, "/");
    if (
      raw.startsWith("/") ||
      raw.includes(":") ||
      raw.split("/").some((part) => part === ".." || HIDDEN.has(part)) ||
      [...raw].some((character) => character.charCodeAt(0) < 32)
    )
      return;
    const slash = raw.lastIndexOf("/");
    const directory = slash < 0 ? "." : raw.slice(0, slash) || ".";
    const term = raw.slice(slash + 1).toLowerCase();
    const controller = new AbortController();
    setLoading(true);
    const timer = window.setTimeout(() => {
      const read = taskID
        ? taskWorkbenchApi
            .scope(taskID, controller.signal)
            .then((scope) =>
              taskWorkbenchApi.tree(
                taskID,
                directory,
                scope.scope_revision,
                controller.signal,
              ),
            )
        : projectsApi.workspaceTree(
            projectID!,
            directory,
            projectRevision!,
            controller.signal,
          );
      void read
        .then((entries) => {
          if (controller.signal.aborted) return;
          setFiles(
            entries
              .filter(
                (entry) =>
                  !entry.path
                    .split("/")
                    .some((part) => HIDDEN.has(part) || part === "..") &&
                  entry.name.toLowerCase().includes(term),
              )
              .sort(
                (a, b) =>
                  Number(b.is_dir) - Number(a.is_dir) ||
                  a.name.localeCompare(b.name),
              )
              .slice(0, 30)
              .map((entry) => ({
                kind: "file",
                key: entry.path,
                label: entry.name,
                path: entry.path,
                directory: entry.is_dir,
              })),
          );
        })
        .catch((err) => {
          if (!controller.signal.aborted)
            setError(
              apiErrorMessage(
                err,
                t("projects.composerResources.filesFailed"),
                t,
              ),
            );
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 200);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [
    query?.kind,
    query?.text,
    taskID,
    projectID,
    projectRevision,
    hostID,
    disabled,
    canReferenceFiles,
    taskWorkbenchApi,
    projectsApi,
    t,
  ]);

  const items = useMemo<ComposerResourceItem[]>(() => {
    if (query?.kind !== "skill") return files;
    if (!resources || !skillsReady) return [];
    const catalog = new Map<
      string,
      { skill: ProjectSkill; name: string; description: string }
    >();
    for (const skill of resources?.skills ?? [])
      catalog.set(projectSkillKey(skill), {
        skill: { package_id: skill.package_id, slug: skill.slug },
        name: skill.name,
        description: skill.description,
      });
    for (const skill of [...inherited, ...(value.skills ?? [])])
      if (!catalog.has(projectSkillKey(skill)))
        catalog.set(projectSkillKey(skill), {
          skill,
          name: "source" in skill ? skill.name : skill.slug,
          description: "source" in skill ? skill.description : "",
        });
    const term = query.text.toLowerCase();
    return [...catalog]
      .filter(
        ([, item]) =>
          item.skill.slug !== "compact" &&
          `${item.name} ${item.skill.slug} ${item.description}`
            .toLowerCase()
            .includes(term),
      )
      .slice(0, 30)
      .map(([key, item]) => ({
        kind: "skill",
        key,
        label: item.name,
        description: item.description,
        skill: item.skill,
        conflict: effective.some(
          (chosen) =>
            chosen.slug === item.skill.slug && projectSkillKey(chosen) !== key,
        ),
      }));
  }, [
    query?.kind,
    query?.text,
    files,
    resources,
    skillsReady,
    inherited,
    value.skills,
    effective,
  ]);

  const replace = (token: string, trailing = " ") => {
    if (!query) return;
    const next = `${text.slice(0, query.start)}${token}${trailing}${text.slice(
      query.end,
    )}`;
    const cursor = query.start + token.length + trailing.length;
    onTextChange(next);
    setQuery(null);
    requestAnimationFrame(() => {
      const input = getInputElement();
      input?.focus();
      input?.setSelectionRange(cursor, cursor);
      setQuery(composerResourceQuery(next, cursor));
      setIndex(0);
    });
  };
  const applySkill = (
    item: Extract<ComposerResourceItem, { kind: "skill" }>,
  ) => {
    const chosen = new Map(
      effective.map((skill) => [projectSkillKey(skill), skill]),
    );
    chosen.set(item.key, item.skill);
    onChange({ ...value, skills: [...chosen.values()] });
    replace(`/${item.skill.slug}`);
    setPendingSkill(null);
  };
  const select = (item: ComposerResourceItem) => {
    if (disabled) return;
    if (item.kind === "skill") {
      if (!skillsReady) return;
      if (item.conflict) return;
      if (value.skills == null && expert?.has_workspace_skills) {
        setPendingSkill(item);
        return;
      }
      applySkill(item);
    } else if (item.directory) {
      const quoted = query?.quoted || /\s/.test(item.path);
      replace(quoted ? `@"${item.path}/` : `@${item.path}/`, "");
    } else
      replace(
        /\s/.test(item.path)
          ? `@${JSON.stringify(item.path)}`
          : `@${item.path}`,
      );
  };
  const open = (kind: "file" | "skill") => {
    if (disabled || (kind === "skill" && !skillsReady)) return;
    const cursor = getInputElement()?.selectionStart ?? text.length;
    const prefix = text.slice(0, cursor);
    const token = `${prefix && !/\s$/.test(prefix) ? " " : ""}${
      kind === "file" ? "@" : "/"
    }`;
    dismissed.current = "";
    const next = `${prefix}${token}${text.slice(cursor)}`;
    onTextChange(next);
    requestAnimationFrame(() => {
      const input = getInputElement();
      input?.focus();
      input?.setSelectionRange(cursor + token.length, cursor + token.length);
      setQuery(composerResourceQuery(next, cursor + token.length));
      setIndex(0);
    });
  };
  const close = () => {
    dismissed.current = JSON.stringify([
      text,
      getInputElement()?.selectionStart ?? text.length,
    ]);
    setQuery(null);
  };
  const onKeyDown = (event: KeyboardEvent): boolean => {
    if (!query || event.nativeEvent.isComposing || event.keyCode === 229)
      return false;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
      return true;
    }
    if (
      (event.key === "ArrowDown" || event.key === "ArrowUp") &&
      items.length
    ) {
      event.preventDefault();
      event.stopPropagation();
      setIndex(
        (current) =>
          (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) %
          items.length,
      );
      return true;
    }
    if (
      (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey)) &&
      (items.length || loading)
    ) {
      event.preventDefault();
      event.stopPropagation();
      if (items.length) select(items[Math.min(index, items.length - 1)]);
      return true;
    }
    return false;
  };
  return {
    query,
    items,
    index,
    setIndex,
    loading: loading || (query?.kind === "skill" && resources === null),
    error,
    disabled,
    canReferenceFiles,
    skillsReady,
    select,
    open,
    close,
    onKeyDown,
    onCursorChange,
    pendingSkill,
    cancelReplacement: () => setPendingSkill(null),
    confirmReplacement: () => pendingSkill && applySkill(pendingSkill),
  };
}

export type TaskComposerResourceController = ReturnType<
  typeof useTaskComposerResources
>;
