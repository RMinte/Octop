import { useTaskHost } from "../../context/TaskHostContext";
import { useRef, useState } from "react";
import { Alert, Button, Input, Space, Spin, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import {
  type InteractionResponse,
  type TaskDetail,
  type TaskInteraction,
} from "../../api/modules/projects";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import { apiErrorMessage } from "../../utils/apiError";

const reviewReasons = new Set([
  "review_allow",
  "review_deny",
  "review_requires_user",
  "review_timeout",
  "review_unavailable",
  "review_context_too_large",
  "review_invalid_response",
  "review_quota_exceeded",
  "review_expired",
  "review_unknown",
]);

export default function TaskInteractions({
  taskID,
  items,
  disabled,
  onChange,
}: {
  taskID: string;
  items: TaskInteraction[];
  disabled: boolean;
  onChange: (detail: TaskDetail) => void;
}) {
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      {items.map((item) => (
        <InteractionCard
          key={item.interaction_id}
          item={item}
          taskID={taskID}
          disabled={disabled}
          onChange={onChange}
        />
      ))}
    </Space>
  );
}

function InteractionCard({
  taskID,
  item,
  disabled,
  onChange,
}: {
  taskID: string;
  item: TaskInteraction;
  disabled: boolean;
  onChange: (detail: TaskDetail) => void;
}) {
  const { projectTasksApi } = useTaskHost();
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [answer, setAnswer] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<{ key: string; body: InteractionResponse } | null>(
    null,
  );
  const inFlight = useRef(false);
  const active = item.status === "pending";
  const reviewReason =
    item.review?.reason_code && reviewReasons.has(item.review.reason_code)
      ? t(`projects.tasks.approvalMode.reviewReason.${item.review.reason_code}`)
      : null;
  const submit = async (response: InteractionResponse["response"]) => {
    if (inFlight.current || !active || disabled) return;
    inFlight.current = true;
    setSending(true);
    setError("");
    const key = JSON.stringify([item.revision, response]);
    if (pending.current?.key !== key)
      pending.current = {
        key,
        body: {
          request_id: crypto.randomUUID(),
          expected_revision: item.revision,
          response,
        },
      };
    try {
      const detail = await projectTasksApi.respond(
        taskID,
        item.turn_id,
        item.interaction_id,
        pending.current.body,
      );
      onChange(detail);
    } catch (err) {
      setError(apiErrorMessage(err, t("projects.tasks.respondFailure"), t));
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  };
  const locked = disabled || sending || !active;
  if (item.kind === "approval" && item.status === "reviewing") {
    return (
      <Alert
        type="info"
        showIcon
        icon={<Spin size="small" />}
        message={t("projects.tasks.approvalMode.reviewing")}
        description={item.payload.tool_name}
      />
    );
  }
  if (
    item.kind === "approval" &&
    item.status !== "pending" &&
    (item.resolution?.source === "auto_reviewer" ||
      item.resolution?.source === "full_access_mode" ||
      item.resolution?.source === "cron_skip_confirmation" ||
      item.resolution?.source === "previous_denial")
  ) {
    return (
      <details>
        <summary>
          {t(`projects.tasks.approvalMode.${item.resolution.source}`)}
          {" · "}
          {item.payload.tool_name}
          {" · "}
          {t(`projects.tasks.interactionOutcome.${item.resolution.outcome}`)}
        </summary>
        <Typography.Paragraph type="secondary">
          {item.payload.reason}
        </Typography.Paragraph>
        {reviewReason && (
          <Typography.Paragraph type="secondary">
            {reviewReason}
          </Typography.Paragraph>
        )}
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {JSON.stringify(item.payload.arguments, null, 2)}
        </pre>
      </details>
    );
  }
  return (
    <Alert
      type={item.kind === "approval" ? "warning" : "info"}
      message={t(
        item.kind === "approval"
          ? "projects.tasks.approvalTitle"
          : "projects.tasks.questionTitle",
      )}
      description={
        <Space direction="vertical" style={{ width: "100%" }}>
          {item.kind === "approval" ? (
            <>
              <Typography.Text>
                {t("projects.tasks.serverAction")}: {item.payload.tool_name}
              </Typography.Text>
              <Typography.Text>{item.payload.reason}</Typography.Text>
              {reviewReason && (
                <Typography.Text type="secondary">
                  {reviewReason}
                </Typography.Text>
              )}
              <pre
                style={{
                  maxHeight: 240,
                  overflow: "auto",
                  whiteSpace: "pre-wrap",
                  overflowWrap: "anywhere",
                }}
              >
                {JSON.stringify(item.payload.arguments, null, 2)}
              </pre>
              {active && (
                <Space>
                  {item.payload.allowed_decisions?.includes("allow_once") && (
                    <Button
                      disabled={locked}
                      loading={sending}
                      onClick={() =>
                        void submit({
                          kind: "approval",
                          decision: "allow_once",
                          action_digest: item.payload.action_digest!,
                        })
                      }
                    >
                      {t("projects.tasks.allowOnce")}
                    </Button>
                  )}
                  <Button
                    danger
                    disabled={locked}
                    onClick={() =>
                      void submit({
                        kind: "approval",
                        decision: "deny",
                        action_digest: item.payload.action_digest!,
                      })
                    }
                  >
                    {t("projects.tasks.deny")}
                  </Button>
                </Space>
              )}
            </>
          ) : (
            <>
              {item.payload.header && (
                <Typography.Text strong>{item.payload.header}</Typography.Text>
              )}
              <Typography.Text>{item.payload.question}</Typography.Text>
              {active && (
                <>
                  <Space wrap>
                    {item.payload.options?.map((option, index) => (
                      <Button
                        key={index}
                        disabled={locked}
                        title={option.description}
                        type={answer === option.label ? "primary" : "default"}
                        onClick={() => setAnswer(option.label)}
                      >
                        {option.label}
                      </Button>
                    ))}
                  </Space>
                  <Input.TextArea
                    value={answer}
                    disabled={locked}
                    maxLength={4000}
                    autoSize={{ minRows: 2, maxRows: 6 }}
                    aria-label={t("projects.tasks.freeAnswer")}
                    placeholder={t("projects.tasks.freeAnswer")}
                    onChange={(event) => setAnswer(event.target.value)}
                  />
                  <Typography.Text type="secondary">
                    {t("projects.tasks.questionHint")}
                  </Typography.Text>
                  <Space wrap>
                    <Button
                      type="primary"
                      loading={sending}
                      disabled={locked || !answer.trim()}
                      onClick={() =>
                        void submit({
                          kind: "question",
                          action: "answer",
                          answer: answer.trim(),
                        })
                      }
                    >
                      {t("projects.tasks.answer")}
                    </Button>
                    {!!item.payload.options?.length && (
                      <Button
                        disabled={locked}
                        onClick={() =>
                          void submit({
                            kind: "question",
                            action: "use_default",
                          })
                        }
                      >
                        {t("projects.tasks.useDefault")}
                      </Button>
                    )}
                    <Button
                      disabled={locked}
                      onClick={() =>
                        void submit({ kind: "question", action: "skip" })
                      }
                    >
                      {t("projects.tasks.skipQuestion")}
                    </Button>
                  </Space>
                </>
              )}
            </>
          )}
          {active ? (
            <Typography.Text type="secondary">
              {t("projects.tasks.expiresAt", {
                time: formatServerIsoDateTime(item.expires_at, timeZone),
              })}
            </Typography.Text>
          ) : (
            <Tag>
              {t(
                `projects.tasks.interactionOutcome.${
                  item.resolution?.outcome ?? item.status
                }`,
              )}
            </Tag>
          )}
          {item.resolution?.answer && (
            <Typography.Text>{item.resolution.answer}</Typography.Text>
          )}
          {error && <Alert type="error" message={error} />}
        </Space>
      }
    />
  );
}
