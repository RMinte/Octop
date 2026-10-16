import { Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type { TaskTurn } from "../../api/modules/projects";
import LazyMarkdown from "../../components/Markdown/LazyMarkdown";
import styles from "./index.module.less";

export default function TaskTurnContent({ turn }: { turn: TaskTurn }) {
  const { t } = useTranslation();
  if (turn.content_blocks.length) {
    return turn.content_blocks.map((block) => {
      const streaming =
        turn.status === "running" && block.status === "streaming";
      if (block.type === "tool") {
        const status = {
          streaming: "running",
          completed: "completed",
          failed: "failed",
          interrupted: "cancelled",
        }[block.status];
        return (
          <div key={block.block_id}>
            <Typography.Text type="secondary">
              {t("projects.tasks.tool", { name: block.tool_name })}
            </Typography.Text>{" "}
            <Tag>{t(`projects.tasks.status.${status}`)}</Tag>
          </div>
        );
      }
      if (block.type === "text")
        return (
          <LazyMarkdown
            key={block.block_id}
            content={block.text}
            isStreaming={streaming}
          />
        );
      return (
        <details key={block.block_id} className={styles.taskReasoning}>
          <summary>
            {t(
              block.type === "reasoning_summary"
                ? "projects.tasks.reasoningSummary"
                : "projects.tasks.reasoning",
            )}
          </summary>
          <LazyMarkdown content={block.text} isStreaming={streaming} />
        </details>
      );
    });
  }
  return (
    <>
      {turn.reasoning && (
        <details className={styles.taskReasoning}>
          <summary>{t("projects.tasks.reasoning")}</summary>
          <LazyMarkdown
            content={turn.reasoning}
            isStreaming={turn.status === "running"}
          />
        </details>
      )}
      {turn.reply && (
        <LazyMarkdown
          content={turn.reply}
          isStreaming={turn.status === "running"}
        />
      )}
      {turn.activity && (
        <Typography.Text type="secondary">
          {t("projects.tasks.tool", { name: turn.activity })}
        </Typography.Text>
      )}
    </>
  );
}
