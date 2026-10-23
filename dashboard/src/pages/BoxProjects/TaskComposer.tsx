import type { HTMLAttributes, ReactNode } from "react";
import TaskExecutionSettings, {
  type TaskExecutionSettingsProps,
} from "./TaskExecutionSettings";
import styles from "./TaskComposer.module.less";

export interface TaskComposerProps extends HTMLAttributes<HTMLDivElement> {
  executionSettings: Omit<TaskExecutionSettingsProps, "children" | "compact">;
  status?: ReactNode;
  attachments?: ReactNode;
  resourceChips?: ReactNode;
  actions?: ReactNode;
  send?: ReactNode;
  identity?: ReactNode;
  mode?: ReactNode;
  webSearch?: ReactNode;
  approval?: ReactNode;
  footer?: ReactNode;
}

export default function TaskComposer({
  executionSettings,
  status,
  attachments,
  resourceChips,
  actions,
  send,
  identity,
  mode,
  webSearch,
  approval,
  footer,
  children,
  className,
  ...rest
}: TaskComposerProps) {
  return (
    <TaskExecutionSettings {...executionSettings} compact>
      {({ model, options, resourceChips: executionChips }) => (
        <div
          {...rest}
          className={[styles.composer, className].filter(Boolean).join(" ")}
        >
          {status && <div className={styles.status}>{status}</div>}
          <div className={styles.inputBox}>
            {attachments}
            {resourceChips}
            {executionChips}
            {children}
            <div className={styles.actions}>
              <div className={styles.actionTools}>{actions}</div>
              <div className={styles.send}>{send}</div>
            </div>
          </div>
          <div className={styles.settings}>
            {identity}
            {model}
            {mode}
            {webSearch}
            {options}
            {approval}
          </div>
          {footer}
        </div>
      )}
    </TaskExecutionSettings>
  );
}
