import { useTaskHost } from "../../context/TaskHostContext";
import { useEffect, useState } from "react";
import { Button, Popover, Space, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";
import {
  type TaskContextUsage as ContextUsage,
  type TaskTurn,
} from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";

const prefix = "projects.tasks.contextUsage";
const segmentKeys: Record<string, string> = {
  system_prompt: "systemPrompt",
  tool_definitions: "tools",
  rules: "rules",
  skills: "skills",
  mcp: "mcp",
  subagent_definitions: "subagents",
  conversation: "conversation",
};
type UsageTurn = Pick<
  TaskTurn,
  "turn_id" | "status" | "input_tokens" | "output_tokens"
>;

function tokens(value: number) {
  return value.toLocaleString();
}

export function TaskTurnUsage({
  turn,
}: {
  turn: Pick<TaskTurn, "input_tokens" | "output_tokens">;
}) {
  const { t } = useTranslation();
  if (turn.input_tokens === undefined && turn.output_tokens === undefined)
    return null;
  return (
    <span>
      {" · "}
      {t(`${prefix}.turnBilling`, {
        input:
          turn.input_tokens === undefined
            ? t(`${prefix}.unknown`)
            : tokens(turn.input_tokens),
        output:
          turn.output_tokens === undefined
            ? t(`${prefix}.unknown`)
            : tokens(turn.output_tokens),
      })}
    </span>
  );
}

export default function TaskContextUsage({
  taskID,
  active,
  turns,
}: {
  taskID: string;
  active: boolean;
  turns: UsageTurn[];
}) {
  const { projectTasksApi } = useTaskHost();
  const { t } = useTranslation();
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{
    taskID: string;
    usage: ContextUsage | null;
    error: unknown;
  }>({ taskID, usage: null, error: null });
  const latest = turns.at(-1);
  const refreshKey = `${latest?.turn_id ?? ""}:${latest?.status ?? ""}`;

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    const controller = new AbortController();
    setState((previous) => ({
      taskID,
      usage: previous.taskID === taskID ? previous.usage : null,
      error: null,
    }));
    const refresh = async () => {
      try {
        const usage = await projectTasksApi.contextUsage(
          taskID,
          controller.signal,
        );
        if (!disposed) setState({ taskID, usage, error: null });
      } catch (error) {
        if (!disposed) setState((previous) => ({ ...previous, taskID, error }));
      } finally {
        if (!disposed && active) timer = window.setTimeout(refresh, 1000);
      }
    };
    void refresh();
    return () => {
      disposed = true;
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [taskID, active, refreshKey, attempt, projectTasksApi]);

  const usage = state.taskID === taskID ? state.usage : null;
  const error = state.taskID === taskID ? state.error : null;
  const measured = turns.filter(
    (turn) =>
      turn.input_tokens !== undefined && turn.output_tokens !== undefined,
  );
  const input = measured.reduce((total, turn) => total + turn.input_tokens!, 0);
  const output = measured.reduce(
    (total, turn) => total + turn.output_tokens!,
    0,
  );
  const missing = turns.length - measured.length;
  const percent =
    usage && usage.max_tokens > 0
      ? usage.used_tokens > 0
        ? Math.max(1, Math.round((usage.used_tokens / usage.max_tokens) * 100))
        : 0
      : null;

  return (
    <Space
      direction="vertical"
      size={2}
      style={{ width: "100%", marginBottom: 12 }}
      aria-label={t(`${prefix}.label`)}
    >
      <Space wrap size="small">
        {!usage && !error && <Spin size="small" />}
        {usage?.source === "empty" && (
          <Typography.Text type="secondary">
            {t(`${prefix}.empty`)}
          </Typography.Text>
        )}
        {usage?.source === "model_request" && (
          <Popover
            trigger="click"
            title={t(`${prefix}.details`)}
            content={
              <Space
                direction="vertical"
                size="small"
                style={{ maxWidth: 320 }}
              >
                <Typography.Text type="secondary">
                  {t(
                    `${prefix}.${
                      usage.estimated ? "estimatedHint" : "observedHint"
                    }`,
                  )}
                </Typography.Text>
                <Typography.Text>
                  {t(
                    `${prefix}.${
                      usage.estimated ? "estimatedTokens" : "requestTokens"
                    }`,
                    {
                      input: tokens(usage.input_tokens),
                      output: tokens(usage.output_tokens),
                    },
                  )}
                </Typography.Text>
                <Typography.Text type="secondary">
                  {t(`${prefix}.segmentsHint`)}
                </Typography.Text>
                {usage.segments.map((segment) => (
                  <Typography.Text key={segment.key}>
                    {t(`${prefix}.segmentTokens`, {
                      label: segmentKeys[segment.key]
                        ? t(`${prefix}.segments.${segmentKeys[segment.key]}`)
                        : segment.key,
                      tokens: tokens(segment.tokens),
                    })}
                  </Typography.Text>
                ))}
              </Space>
            }
          >
            <Button
              type="text"
              size="small"
              aria-label={t(`${prefix}.details`)}
            >
              {t(`${prefix}.occupancy`, {
                used: tokens(usage.used_tokens),
                max:
                  usage.max_tokens > 0
                    ? tokens(usage.max_tokens)
                    : t(`${prefix}.unknown`),
              })}
              {percent !== null ? ` · ${percent}%` : ""}
              {usage.estimated ? ` · ${t(`${prefix}.estimated`)}` : ""}
            </Button>
          </Popover>
        )}
        {!!error && (
          <>
            <Typography.Text type="danger">
              {apiErrorMessage(error, t(`${prefix}.failure`), t)}
            </Typography.Text>
            <Button
              size="small"
              onClick={() => setAttempt((value) => value + 1)}
            >
              {t(`${prefix}.retry`)}
            </Button>
          </>
        )}
      </Space>
      <Typography.Text type="secondary">
        {measured.length
          ? t(`${prefix}.loadedBilling`, {
              count: turns.length,
              input: tokens(input),
              output: tokens(output),
            })
          : t(`${prefix}.loadedEmpty`, { count: turns.length })}
        {measured.length > 0 && missing > 0
          ? ` · ${t(`${prefix}.missingBilling`, { count: missing })}`
          : ""}
      </Typography.Text>
    </Space>
  );
}
