import { Tag, Typography } from "antd";
import {
  Circle,
  CircleCheck,
  CircleDot,
  CirclePause,
  CircleSlash,
  MessageCircle,
  Wrench,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type {
  TaskContentBlock,
  TaskPlanStep,
  TaskTurn,
} from "../../api/modules/projects";
import LazyMarkdown from "../../components/Markdown/LazyMarkdown";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import styles from "./index.module.less";

function elapsedMilliseconds(start?: string | null, finish?: string | null) {
  if (!start || !finish) return null;
  const duration = Date.parse(finish) - Date.parse(start);
  return Number.isFinite(duration) && duration >= 0 ? duration : null;
}

function TaskTiming({
  startedAt,
  finishedAt,
  removedAt,
  duration,
  timeZone,
}: {
  startedAt?: string | null;
  finishedAt?: string | null;
  removedAt?: string | null;
  duration?: number | null;
  timeZone: string;
}) {
  const { t } = useTranslation();
  const milliseconds = duration ?? elapsedMilliseconds(startedAt, finishedAt);
  const hasDuration =
    milliseconds != null && Number.isFinite(milliseconds) && milliseconds >= 0;
  if (!startedAt && !finishedAt && !removedAt && !hasDuration) return null;
  return (
    <dl className={styles.taskTiming}>
      {(
        [
          ["started", startedAt],
          ["finished", finishedAt],
          ["removed", removedAt],
        ] as const
      ).map(
        ([label, timestamp]) =>
          timestamp && (
            <div key={label}>
              <dt>{t(`projects.tasks.actionDetails.${label}`)}</dt>
              <dd>
                <time dateTime={timestamp}>
                  {formatServerIsoDateTime(timestamp, timeZone)}
                </time>
              </dd>
            </div>
          ),
      )}
      {hasDuration && (
        <div>
          <dt>{t("projects.tasks.actionDetails.duration")}</dt>
          <dd>
            {t("projects.tasks.actionDetails.milliseconds", {
              value: milliseconds,
            })}
          </dd>
        </div>
      )}
    </dl>
  );
}

function TaskPlanStepContent({
  step,
  blocks,
  turn,
  timeZone,
}: {
  step: TaskPlanStep;
  blocks: TaskContentBlock[];
  turn: TaskTurn;
  timeZone: string;
}) {
  const { t } = useTranslation();
  const Icon = {
    pending: Circle,
    in_progress: CircleDot,
    completed: CircleCheck,
    blocked: CirclePause,
    skipped: CircleSlash,
  }[step.status];
  return (
    <li data-status={step.status}>
      <details
        className={styles.taskPlanStep}
        data-step-id={step.step_id || undefined}
        open={step.status === "in_progress" && !step.removed_at}
      >
        <summary>
          <span
            className={styles.taskPlanMarker}
            role="img"
            aria-label={t(`projects.tasks.plan.stepStatus.${step.status}`)}
          >
            <Icon size={16} aria-hidden="true" />
          </span>
          <span className={styles.taskPlanStepTitle}>{step.content}</span>
          <Tag>{t(`projects.tasks.plan.stepStatus.${step.status}`)}</Tag>
          {!!blocks.length && (
            <span className={styles.taskPlanRecordCount}>
              {t("projects.tasks.plan.records", { count: blocks.length })}
            </span>
          )}
        </summary>
        <div className={styles.taskPlanStepBody}>
          <TaskTiming
            startedAt={step.started_at}
            finishedAt={step.finished_at}
            removedAt={step.removed_at}
            timeZone={timeZone}
          />
          {step.result && (
            <div className={styles.taskStepResult}>
              <Typography.Text strong>
                {t("projects.tasks.plan.result")}
              </Typography.Text>
              <LazyMarkdown content={step.result} />
            </div>
          )}
          {!!blocks.length && (
            <TaskActions turn={turn} blocks={blocks} timeZone={timeZone} />
          )}
          {!blocks.length && !step.result && (
            <Typography.Text type="secondary">
              {t("projects.tasks.plan.noRecords")}
            </Typography.Text>
          )}
        </div>
      </details>
    </li>
  );
}

function TaskPlanCard({
  turn,
  actions,
  timeZone,
}: {
  turn: TaskTurn;
  actions: Map<string, TaskContentBlock[]>;
  timeZone: string;
}) {
  const { t } = useTranslation();
  const plan = turn.plan;
  if (!plan) return null;
  const completed = plan.steps.filter(
    (step) => step.status === "completed",
  ).length;
  const current = plan.steps.filter((step) => step.status === "in_progress");
  const renderSteps = (steps: TaskPlanStep[]) => (
    <ol className={styles.taskPlanSteps}>
      {steps.map((step, index) => (
        <TaskPlanStepContent
          key={step.step_id || `historic-${index}`}
          step={step}
          blocks={step.step_id ? actions.get(step.step_id) ?? [] : []}
          turn={turn}
          timeZone={timeZone}
        />
      ))}
    </ol>
  );
  return (
    <details className={styles.taskPlan} open={turn.status !== "completed"}>
      <summary>
        <span className={styles.taskPlanHeading}>
          <strong>{plan.title || t("projects.tasks.plan.title")}</strong>
          <span>
            {t("projects.tasks.plan.count", {
              completed,
              total: plan.steps.length,
            })}
          </span>
          <Tag>{t(`projects.tasks.status.${turn.status}`)}</Tag>
        </span>
        {current.map((step, index) => (
          <span className={styles.taskPlanCurrent} key={step.step_id || index}>
            {t("projects.tasks.plan.current", { content: step.content })}
          </span>
        ))}
      </summary>
      {plan.steps.length ? (
        renderSteps(plan.steps)
      ) : (
        <Typography.Text type="secondary">
          {t("projects.tasks.plan.empty")}
        </Typography.Text>
      )}
      {!!plan.retired_steps?.length && (
        <div className={styles.taskRetiredSteps}>
          <Typography.Text type="secondary">
            {t("projects.tasks.plan.retired")}
          </Typography.Text>
          {renderSteps(plan.retired_steps)}
        </div>
      )}
    </details>
  );
}

const processBlock = (block: TaskContentBlock) =>
  block.type !== "plan" && block.type !== "text";

type ContentGroup =
  | { type: "text"; block: TaskContentBlock }
  | { type: "actions"; blocks: TaskContentBlock[] };
function groupContent(blocks: TaskContentBlock[]): ContentGroup[] {
  const groups: ContentGroup[] = [];
  for (const block of blocks) {
    if (block.type === "plan") continue;
    if (block.type === "text") groups.push({ type: "text", block });
    else {
      const previous = groups[groups.length - 1];
      if (previous?.type === "actions") previous.blocks.push(block);
      else groups.push({ type: "actions", blocks: [block] });
    }
  }
  return groups;
}

function toolStatus(block: TaskContentBlock, turn: TaskTurn) {
  if (block.status === "failed") return "failed";
  const nativeResult =
    block.status === "completed" &&
    block.output != null &&
    (block.tool_name?.startsWith("client__") ||
      block.tool_name === "ask_user_question");
  if (nativeResult) return "completed";
  if (!block.started_at) {
    return block.status === "streaming" &&
      [
        "queued",
        "waiting_approval",
        "waiting_answer",
        "waiting_client_tool",
        "paused",
      ].includes(turn.status)
      ? turn.status
      : "unconfirmed";
  }
  return {
    streaming: "running",
    completed: "completed",
    failed: "failed",
    interrupted: "cancelled",
  }[block.status];
}

const structuredText = (value: unknown) =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2);

function TaskActions({
  turn,
  blocks,
  timeZone,
}: {
  turn: TaskTurn;
  blocks: TaskContentBlock[];
  timeZone: string;
}) {
  const { t } = useTranslation();
  const hasActions = blocks.some(
    (block) => block.type === "tool" || block.type === "progress",
  );
  return (
    <section
      className={hasActions ? styles.taskActions : undefined}
      aria-label={hasActions ? t("projects.tasks.actions") : undefined}
    >
      {hasActions && (
        <Typography.Text type="secondary">
          {t("projects.tasks.actions")}
        </Typography.Text>
      )}
      <ol className={styles.taskActionChain}>
        {blocks.map((block) => {
          const streaming =
            turn.status === "running" && block.status === "streaming";
          if (block.type === "tool")
            return (
              <li key={block.block_id} className={styles.taskAction}>
                <Wrench size={15} aria-hidden="true" />
                <details className={styles.taskTool}>
                  <summary className={styles.taskActionHeading}>
                    <Typography.Text>
                      {block.tool_label ||
                        t("projects.tasks.tool", { name: block.tool_name })}
                    </Typography.Text>
                    <Tag>
                      {t(
                        `projects.tasks.actionStatus.${toolStatus(
                          block,
                          turn,
                        )}`,
                      )}
                    </Tag>
                  </summary>
                  <TaskTiming
                    startedAt={block.started_at}
                    finishedAt={block.finished_at}
                    duration={block.duration_ms}
                    timeZone={timeZone}
                  />
                  {block.text && (
                    <LazyMarkdown
                      content={block.text}
                      isStreaming={streaming}
                    />
                  )}
                  <Typography.Text strong>
                    {t("projects.tasks.actionDetails.arguments")}
                  </Typography.Text>
                  {block.arguments != null ? (
                    <pre
                      className={styles.taskToolData}
                      aria-label={t("projects.tasks.actionDetails.arguments")}
                    >
                      {structuredText(block.arguments)}
                    </pre>
                  ) : (
                    <p>{t("projects.tasks.actionDetails.noArguments")}</p>
                  )}
                  <Typography.Text strong>
                    {t("projects.tasks.actionDetails.output")}
                  </Typography.Text>
                  {block.output != null ? (
                    <pre
                      className={styles.taskToolData}
                      aria-label={t("projects.tasks.actionDetails.output")}
                    >
                      {structuredText(block.output)}
                    </pre>
                  ) : (
                    <p>{t("projects.tasks.actionDetails.noOutput")}</p>
                  )}
                </details>
              </li>
            );
          if (block.type === "progress")
            return (
              <li key={block.block_id} className={styles.taskAction}>
                <MessageCircle size={15} aria-hidden="true" />
                <div className={styles.taskActionBody}>
                  <LazyMarkdown content={block.text} isStreaming={streaming} />
                </div>
              </li>
            );
          return (
            <li key={block.block_id}>
              <details className={styles.taskReasoning}>
                <summary>
                  {t(
                    block.type === "reasoning_summary"
                      ? "projects.tasks.reasoningSummary"
                      : "projects.tasks.reasoning",
                  )}
                </summary>
                <LazyMarkdown content={block.text} isStreaming={streaming} />
              </details>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export default function TaskTurnContent({ turn }: { turn: TaskTurn }) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const plan = turn.plan;
  const stepIDs = new Set(
    [...(plan?.steps ?? []), ...(plan?.retired_steps ?? [])]
      .map((step) => step.step_id)
      .filter(Boolean),
  );
  const actions = new Map<string, TaskContentBlock[]>();
  const unscoped = turn.content_blocks.filter((block) => {
    if (
      !processBlock(block) ||
      !plan?.plan_id ||
      block.plan_id !== plan.plan_id ||
      !block.step_id ||
      !stepIDs.has(block.step_id)
    )
      return true;
    const existing = actions.get(block.step_id) ?? [];
    existing.push(block);
    actions.set(block.step_id, existing);
    return false;
  });
  return (
    <>
      <TaskPlanCard turn={turn} actions={actions} timeZone={timeZone} />
      {turn.content_blocks.length ? (
        groupContent(unscoped).map((group) =>
          group.type === "text" ? (
            <LazyMarkdown
              key={group.block.block_id}
              content={group.block.text}
              isStreaming={
                turn.status === "running" && group.block.status === "streaming"
              }
            />
          ) : (
            <TaskActions
              key={group.blocks[0].block_id}
              turn={turn}
              blocks={group.blocks}
              timeZone={timeZone}
            />
          ),
        )
      ) : (
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
      )}
    </>
  );
}
