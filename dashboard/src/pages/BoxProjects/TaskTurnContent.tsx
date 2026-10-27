import TaskSourceTrajectory from "./TaskSourceTrajectory";
import { Button, Tag, Typography } from "antd";
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
import { parseKnowledgeCitations } from "../../utils/parseKnowledgeCitations";
import TaskFileChanges from "./TaskFileChanges";
import TaskKnowledgeCitations from "./TaskKnowledgeCitations";
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
  liveDetail,
  timeZone,
}: {
  step: TaskPlanStep;
  liveDetail?: string;
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
          {liveDetail && (
            <Typography.Paragraph
              type="secondary"
              className={styles.taskPlanLiveDetail}
            >
              {liveDetail}
            </Typography.Paragraph>
          )}
          {!liveDetail && !step.result && (
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
  timeZone,
  onOpenArtifacts,
}: {
  turn: TaskTurn;
  timeZone: string;
  onOpenArtifacts?: () => void;
}) {
  const { t } = useTranslation();
  const plan = turn.plan;
  if (!plan) return null;
  const completed = plan.steps.filter(
    (step) => step.status === "completed" || step.status === "skipped",
  ).length;
  const current = plan.steps.find(
    (step) =>
      step.status === "in_progress" &&
      (!plan.active_step_id || step.step_id === plan.active_step_id),
  );
  const live = [
    "queued",
    "running",
    "waiting_approval",
    "waiting_answer",
    "waiting_client_tool",
  ].includes(turn.status);
  const latestActivity =
    plan.plan_id && current?.step_id
      ? [...turn.content_blocks]
          .reverse()
          .find(
            (block) =>
              block.plan_id === plan.plan_id &&
              block.step_id === current.step_id &&
              (block.type === "progress" || block.type === "tool"),
          )
      : undefined;
  const activityText = live
    ? (latestActivity?.type === "progress"
        ? latestActivity.text
        : latestActivity?.brief || latestActivity?.tool_label || ""
      )
        .replace(/\s+/g, " ")
        .trim()
    : "";
  const liveDetail =
    activityText.length > 160 ? `${activityText.slice(0, 160)}…` : activityText;
  const renderSteps = (steps: TaskPlanStep[]) => (
    <ol className={styles.taskPlanSteps}>
      {steps.map((step, index) => (
        <TaskPlanStepContent
          key={step.step_id || `historic-${index}`}
          step={step}
          liveDetail={step === current && !step.removed_at ? liveDetail : ""}
          timeZone={timeZone}
        />
      ))}
    </ol>
  );
  return (
    <div>
      <details
        className={styles.taskPlan}
        open={completed !== plan.steps.length || !plan.steps.length}
      >
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
          {current && (
            <span className={styles.taskPlanCurrent}>
              {t("projects.tasks.plan.current", { content: current.content })}
            </span>
          )}
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
      {!!turn.artifacts.length && onOpenArtifacts && (
        <Button type="link" onClick={onOpenArtifacts}>
          {t("projects.tasks.plan.openArtifacts", {
            count: turn.artifacts.length,
          })}
        </Button>
      )}
    </div>
  );
}

const processBlock = (block: TaskContentBlock) =>
  block.type !== "plan" && block.type !== "text";

function toolStatus(block: TaskContentBlock, turn: TaskTurn) {
  if (block.status === "failed") return "failed";
  if (
    turn.execution_engine?.engine_id === "acp" &&
    block.status === "completed"
  )
    return "completed";
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
  structured,
  onOpenToolUI,
}: {
  turn: TaskTurn;
  blocks: TaskContentBlock[];
  timeZone: string;
  structured: boolean;
  onOpenToolUI?: (id: string) => void;
}) {
  const { t } = useTranslation();
  const label = t(
    structured ? "projects.tasks.actions" : "projects.tasks.thinking",
  );
  return (
    <section
      className={structured ? styles.taskActions : styles.taskThinking}
      aria-label={label}
    >
      <Typography.Text type="secondary">{label}</Typography.Text>
      <ol
        className={
          structured ? styles.taskActionChain : styles.taskThinkingItems
        }
      >
        {blocks.map((block) => {
          const streaming =
            turn.status === "running" && block.status === "streaming";
          if (block.type === "tool")
            return (
              <li key={block.block_id} className={styles.taskAction}>
                {structured && <Wrench size={15} aria-hidden="true" />}
                <details className={styles.taskTool}>
                  <summary className={styles.taskActionHeading}>
                    <Typography.Text>
                      {block.brief ||
                        block.tool_label ||
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
                  {block.speaker_name && <Tag>{block.speaker_name}</Tag>}
                  {onOpenToolUI && (
                    <Button
                      size="small"
                      onClick={() =>
                        onOpenToolUI(block.tool_call_id ?? block.block_id)
                      }
                    >
                      {t("projects.tasks.workbench.openTool")}
                    </Button>
                  )}
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
                      {block.tool_name === "search_knowledge" &&
                      typeof block.output === "string"
                        ? parseKnowledgeCitations(block.output).text
                        : structuredText(block.output)}
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
                {structured && <MessageCircle size={15} aria-hidden="true" />}
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

export default function TaskTurnContent({
  turn,
  onOpenArtifacts,
  onOpenToolUI,
}: {
  turn: TaskTurn;
  onOpenArtifacts?: () => void;
  onOpenToolUI?: (id: string) => void;
}) {
  const timeZone = useServerTimezone();
  // Older cached turns use their saved execution option and a fixed panel default.
  const settings = turn.presentation ?? {
    action_timeline_enabled: turn.options.action_timeline_enabled ?? true,
    step_panel_enabled: true,
  };
  const showPlan = settings.step_panel_enabled && turn.plan != null;
  const process = turn.content_blocks.filter(
    (block) =>
      processBlock(block) &&
      (!settings.action_timeline_enabled ||
        block.type === "progress" ||
        block.type === "tool"),
  );
  if (!turn.content_blocks.length) {
    if (!settings.action_timeline_enabled && turn.reasoning) {
      process.push({
        block_id: "legacy-reasoning",
        type: "reasoning",
        text: turn.reasoning,
        status: turn.status === "running" ? "streaming" : "completed",
        tool_call_id: null,
        tool_name: null,
      });
    }
    if (turn.activity) {
      process.push({
        block_id: "legacy-activity",
        type: "tool",
        text: "",
        status: turn.status === "running" ? "streaming" : "completed",
        tool_call_id: null,
        tool_name: turn.activity,
      });
    }
  }
  const answers = turn.content_blocks.filter((block) => block.type === "text");
  const citations = [
    ...new Map(
      turn.content_blocks.flatMap((block) =>
        block.type === "tool" &&
        block.tool_name === "search_knowledge" &&
        typeof block.output === "string"
          ? parseKnowledgeCitations(block.output).citations.map(
              (citation) =>
                [`${citation.kbId}:${citation.docId}`, citation] as const,
            )
          : [],
      ),
    ).values(),
  ];
  return (
    <>
      {showPlan ? (
        <TaskPlanCard
          turn={turn}
          timeZone={timeZone}
          onOpenArtifacts={onOpenArtifacts}
        />
      ) : process.length > 0 ? (
        <TaskActions
          turn={turn}
          blocks={process}
          timeZone={timeZone}
          structured={settings.action_timeline_enabled}
          onOpenToolUI={onOpenToolUI}
        />
      ) : null}
      {answers.map((answer) => (
        <div key={answer.block_id}>
          {answer.speaker_name && (
            <Typography.Text type="secondary">
              {answer.speaker_name}
            </Typography.Text>
          )}
          <LazyMarkdown
            content={parseKnowledgeCitations(answer.text).text}
            isStreaming={
              turn.status === "running" && answer.status === "streaming"
            }
          />
        </div>
      ))}
      {!answers.length && turn.reply && (
        <LazyMarkdown
          content={parseKnowledgeCitations(turn.reply).text}
          isStreaming={turn.status === "running"}
        />
      )}
      <TaskKnowledgeCitations citations={citations} />
      <TaskFileChanges key={turn.turn_id} changes={turn.file_changes ?? []} />
      <TaskSourceTrajectory items={turn.source_trajectory ?? []} />
    </>
  );
}
