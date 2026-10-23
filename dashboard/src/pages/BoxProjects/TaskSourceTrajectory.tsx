import { Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type { TaskSourceEvent } from "../../api/modules/projects";

/** Public source evidence is not an executable Task interaction or checkpoint. */
export default function TaskSourceTrajectory({
  items,
}: {
  items: TaskSourceEvent[];
}) {
  const { t } = useTranslation();
  if (!items.length) return null;
  return (
    <details>
      <summary>
        {t("projects.tasks.sourceTrajectory.title", { count: items.length })}
      </summary>
      <Typography.Paragraph type="secondary">
        {t("projects.tasks.sourceTrajectory.hint")}
      </Typography.Paragraph>
      <ol>
        {items.map((event, index) => (
          <li key={event.source_event_id ?? index}>
            <Tag>{event.kind}</Tag>
            <Tag>
              {t(`projects.tasks.sourceTrajectory.${event.source_status}`)}
            </Tag>
            {event.speaker != null && (
              <Typography.Text type="secondary">
                {typeof event.speaker === "string"
                  ? event.speaker
                  : JSON.stringify(event.speaker)}
              </Typography.Text>
            )}
            {event.summary && (
              <Typography.Paragraph>{event.summary}</Typography.Paragraph>
            )}
            {event.source_status !== "redacted" && event.payload != null && (
              <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {typeof event.payload === "string"
                  ? event.payload
                  : JSON.stringify(event.payload, null, 2)}
              </pre>
            )}
          </li>
        ))}
      </ol>
    </details>
  );
}
