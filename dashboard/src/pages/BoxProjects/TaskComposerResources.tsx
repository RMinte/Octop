import { Button, Modal, Spin } from "antd";
import { AtSign, FileText, Folder, Sparkles, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { TaskComposerResourceController } from "./useTaskComposerResources";
import styles from "./TaskComposerResources.module.less";

export function TaskComposerResourceTriggers({
  controller,
}: {
  controller: TaskComposerResourceController;
}) {
  const { t } = useTranslation();
  return (
    <>
      <Button
        size="small"
        type="text"
        icon={<AtSign size={15} />}
        disabled={controller.disabled || !controller.canReferenceFiles}
        title={t(
          `projects.composerResources.${
            controller.canReferenceFiles ? "files" : "filesUnavailableDraft"
          }`,
        )}
        onClick={() => controller.open("file")}
      >
        {t("projects.composerResources.files")}
      </Button>
      <Button
        size="small"
        type="text"
        icon={<Sparkles size={15} />}
        disabled={controller.disabled || !controller.skillsReady}
        title={
          !controller.skillsReady
            ? t("projects.composerResources.skillDefaultsUnavailable")
            : undefined
        }
        onClick={() => controller.open("skill")}
      >
        {t("projects.composerResources.skills")}
      </Button>
    </>
  );
}

export default function TaskComposerResources({
  controller: c,
}: {
  controller: TaskComposerResourceController;
}) {
  const { t } = useTranslation();
  return (
    <>
      {c.query && (
        <div
          className={styles.picker}
          role="region"
          aria-label={t(
            `projects.composerResources.${
              c.query.kind === "file" ? "files" : "skills"
            }`,
          )}
        >
          <div className={styles.heading}>
            <strong>
              {t(
                `projects.composerResources.${
                  c.query.kind === "file" ? "files" : "skills"
                }`,
              )}
            </strong>
            <Button
              type="text"
              size="small"
              icon={<X size={14} />}
              aria-label={t("common.close")}
              onClick={c.close}
            />
          </div>
          {c.query.kind === "file" && !c.canReferenceFiles ? (
            <p>{t("projects.composerResources.filesUnavailableDraft")}</p>
          ) : (
            <>
              <div className={styles.hint}>
                {t(
                  `projects.composerResources.${
                    c.query.kind === "file" ? "filesHint" : "skillsHint"
                  }`,
                )}
              </div>
              {c.loading && <Spin size="small" />}
              {c.error && <p role="alert">{c.error}</p>}
              {c.query.kind === "skill" && !c.skillsReady && !c.loading && (
                <p role="status">
                  {t("projects.composerResources.skillDefaultsUnavailable")}
                </p>
              )}
              <div className={styles.list} role="listbox">
                {c.items.map((item, index) => (
                  <button
                    key={item.key}
                    type="button"
                    role="option"
                    aria-selected={index === c.index}
                    disabled={item.kind === "skill" && item.conflict}
                    className={`${styles.item} ${
                      index === c.index ? styles.active : ""
                    }`}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => c.setIndex(index)}
                    onClick={() => c.select(item)}
                  >
                    {item.kind === "skill" ? (
                      <Sparkles size={16} />
                    ) : item.directory ? (
                      <Folder size={16} />
                    ) : (
                      <FileText size={16} />
                    )}
                    <span>
                      <strong>{item.label}</strong>
                      <small>
                        {item.kind === "file"
                          ? item.path + (item.directory ? "/" : "")
                          : `/${item.skill.slug} · ${
                              "source" in item.skill
                                ? "SkillHub"
                                : item.skill.package_id
                            }${
                              item.conflict
                                ? ` · ${t(
                                    "projects.composerResources.skillConflict",
                                  )}`
                                : ""
                            }`}
                      </small>
                    </span>
                  </button>
                ))}
              </div>
              {!c.loading &&
                !c.error &&
                !c.items.length &&
                (c.query.kind !== "skill" || c.skillsReady) && (
                  <p>{t("projects.composerResources.empty")}</p>
                )}
            </>
          )}
        </div>
      )}
      <Modal
        open={!!c.pendingSkill}
        title={t("projects.composerResources.replaceWorkspaceSkillsTitle")}
        okText={t("projects.composerResources.replaceWorkspaceSkillsConfirm")}
        cancelText={t("common.cancel")}
        onOk={c.confirmReplacement}
        onCancel={c.cancelReplacement}
      >
        {t("projects.composerResources.replaceWorkspaceSkills")}
      </Modal>
    </>
  );
}
