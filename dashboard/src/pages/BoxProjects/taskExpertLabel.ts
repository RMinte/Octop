import type { TaskResources } from "../../api/modules/projects";
export function taskExpertLabel(
  expert: NonNullable<TaskResources["experts"]>[number],
  t: (key: string) => string,
): string {
  return [
    expert.name,
    expert.kind === "team" ? t("projects.tasks.teamExpert") : null,
    expert.is_shared && expert.is_owner === false
      ? t("projects.tasks.sharedExpert")
      : null,
  ]
    .filter(Boolean)
    .join(" · ");
}
