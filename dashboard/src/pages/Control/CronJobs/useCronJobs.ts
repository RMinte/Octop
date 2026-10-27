import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslation } from "react-i18next";
import { octopCronApi } from "../../../api/modules/cronjob";
import type {
  CronConversationPolicy,
  CronJobSpecOutput,
  OctopCronRow,
} from "../../../api/types";
import { channelFromSessionKey } from "./cronDisplay";
import { presetToCron, cronToPreset } from "./components/constants";
import { message } from "@/utils/antdMessage";

import {
  defaultModelFromForm,
  defaultModelToForm,
} from "../../../utils/modelOptions";

type CronJob = CronJobSpecOutput;

/** Octop-aligned form values for create / edit drawer. */
export interface CronJobFormValues {
  id?: string;
  project_id?: string | null;
  selected_expert_id?: string | null;
  conversation_policy?: CronConversationPolicy;
  target_task_id?: string | null;
  name: string;
  enabled: boolean;
  skip_confirmation: boolean;
  schedule: {
    type: "cron";
    cron?: string;
    timezone: string;
  };
  _scheduleMode?: "preset" | "custom" | "agently";
  mail_instance_id?: string;
  _preset?: string;
  prompt: string;
  task_type: "text" | "agent";
  model?: string;
  fresh_thread: boolean;
  session_key?: string | null;
  mcp_servers?: string[];
}

function promptLabel(prompt: string, id: string): string {
  const text = prompt.trim();
  if (!text) return id;
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
}

function fromOctop(row: OctopCronRow, timezone: string): CronJob {
  const idx = row.trigger.indexOf(":");
  const kind = idx >= 0 ? row.trigger.slice(0, idx) : "";
  const value = idx >= 0 ? row.trigger.slice(idx + 1) : row.trigger;
  const cron = kind === "cron" ? value : row.trigger;
  const channel = channelFromSessionKey(row.session_key);

  return {
    id: row.id,
    project_id: row.project_id ?? null,
    selected_expert_id:
      row.selected_expert_id !== undefined
        ? row.selected_expert_id
        : row.agent_id,
    name: row.name?.trim() || promptLabel(row.prompt, row.id),
    enabled: row.enabled,
    skip_confirmation: row.skip_confirmation ?? false,
    schedule: {
      type: "cron",
      cron,
      timezone,
    },
    task_type: row.task_type === "text" ? "text" : "agent",
    model: row.model ?? undefined,
    request: {
      input: [
        {
          role: "user",
          content: [{ text: row.prompt || "", type: "text" }],
        },
      ],
    },
    dispatch: {
      type: "channel",
      channel,
      target: {
        user_id: "admin",
        session_id: "default",
      },
      mode: "final",
    },
    meta: {
      orca_agent_id: row.agent_id,
      octop_conversation_policy: row.conversation_policy ?? "continue_target",
      octop_target_task_id: row.target_task_id ?? null,
      octop_fresh_thread: row.fresh_thread,
      octop_session_key: row.session_key,
      octop_model: row.model,
      octop_task_type: row.task_type,
      octop_mcp_servers: row.mcp_servers ?? [],
      octop_last_run_at: row.last_run_at,
      octop_last_status: row.last_status,
      octop_last_error: row.last_error,
    },
  };
}

export function jobToFormValues(
  job: CronJob,
  timezone: string,
): CronJobFormValues {
  const meta = (job.meta as Record<string, unknown> | undefined) ?? {};
  const input = job.request?.input;
  let prompt = "";
  if (Array.isArray(input) && input.length > 0) {
    const last = input[input.length - 1] as
      | { content?: Array<{ type: string; text: string }> }
      | undefined;
    const part = last?.content?.find?.((c) => c.type === "text");
    if (part?.text) prompt = part.text;
  }
  const idx = (job.schedule?.cron || "").indexOf(":");
  const cronExpr =
    idx >= 0 && job.schedule?.cron?.startsWith("cron:")
      ? job.schedule.cron.slice(idx + 1)
      : job.schedule?.cron || "";

  const matchedPreset = cronToPreset(cronExpr);
  return {
    id: job.id,
    project_id: job.project_id ?? null,
    selected_expert_id: job.selected_expert_id ?? null,
    conversation_policy:
      (meta.octop_conversation_policy as CronConversationPolicy) ??
      "continue_target",
    target_task_id:
      typeof meta.octop_target_task_id === "string"
        ? meta.octop_target_task_id
        : null,
    name: job.name || "",
    enabled: Boolean(job.enabled),
    skip_confirmation: job.skip_confirmation ?? false,
    schedule: {
      type: "cron",
      cron: cronExpr,
      timezone,
    },
    prompt,
    task_type: job.task_type === "text" ? "text" : "agent",
    model: defaultModelToForm(
      job.model ??
        (typeof meta.octop_model === "string" ? meta.octop_model : undefined),
    ),
    fresh_thread: Boolean(meta.octop_fresh_thread),
    session_key:
      typeof meta.octop_session_key === "string"
        ? meta.octop_session_key
        : null,
    mcp_servers: Array.isArray(meta.octop_mcp_servers)
      ? (meta.octop_mcp_servers as string[])
      : [],
    _scheduleMode: cronExpr.startsWith("agently:")
      ? "agently"
      : matchedPreset
      ? "preset"
      : "custom",
    mail_instance_id: cronExpr.startsWith("agently:")
      ? cronExpr.slice(8)
      : undefined,
    _preset: matchedPreset || "daily_9am",
  };
}

function resolveCronExpression(values: CronJobFormValues): string {
  if (values._scheduleMode === "agently") {
    return `agently:${values.mail_instance_id || ""}`;
  }
  if (values._scheduleMode === "preset" && values._preset) {
    const presetCron = presetToCron(values._preset);
    if (presetCron) return `cron:${presetCron}`;
  }
  const cron = values.schedule?.cron || "";
  return /^(cron|interval|date|agently):/.test(cron) ? cron : `cron:${cron}`;
}

function toOctopCreateBody(values: CronJobFormValues) {
  return {
    project_id: values.project_id ?? null,
    selected_expert_id: values.selected_expert_id || null,
    conversation_policy: values.conversation_policy ?? "per_job",
    target_task_id: values.target_task_id || null,
    name: (values.name || "").trim(),
    trigger: resolveCronExpression(values),
    prompt: values.prompt.trim(),
    task_type: values.task_type,
    session_key: values.fresh_thread ? null : values.session_key || null,
    fresh_thread: Boolean(values.fresh_thread),
    enabled: Boolean(values.enabled),
    skip_confirmation: Boolean(values.skip_confirmation),
    model: defaultModelFromForm(values.model),
    mcp_servers: values.mcp_servers ?? [],
  };
}

function toOctopPatchBody(values: CronJobFormValues) {
  return {
    ...toOctopCreateBody(values),
    enabled: Boolean(values.enabled),
  };
}

export function useCronJobs() {
  const { t } = useTranslation();
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [cronTimezone, setCronTimezone] = useState("UTC");
  const fetchGenRef = useRef(0);
  const timezoneRef = useRef(cronTimezone);
  timezoneRef.current = cronTimezone;

  useEffect(() => {
    let cancelled = false;
    void octopCronApi
      .settings()
      .then((settings) => {
        if (!cancelled) setCronTimezone(settings.timezone || "UTC");
      })
      .catch((error) => console.error("Failed to load cron settings", error));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setJobs((previous) =>
      previous.map((job) => ({
        ...job,
        schedule: { ...job.schedule, timezone: cronTimezone },
      })),
    );
  }, [cronTimezone]);

  const fetchJobs = useCallback(
    async (soft = true) => {
      const generation = ++fetchGenRef.current;
      if (soft) setRefreshing(true);
      else setLoading(true);
      try {
        const rows = await octopCronApi.list();
        if (generation === fetchGenRef.current) {
          setJobs(rows.map((row) => fromOctop(row, timezoneRef.current)));
        }
      } catch (error) {
        if (generation === fetchGenRef.current) {
          console.error("Failed to load cron jobs", error);
          message.error(t("cronJobs.loadFailed"));
        }
      } finally {
        if (generation === fetchGenRef.current) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [t],
  );

  useEffect(() => {
    const generation = fetchGenRef;
    void fetchJobs(false);
    return () => {
      generation.current++;
    };
  }, [fetchJobs]);

  const saveRow = (row: OctopCronRow) => {
    // Discard a list request started before this mutation.
    fetchGenRef.current++;
    setLoading(false);
    setRefreshing(false);
    const job = fromOctop(row, timezoneRef.current);
    setJobs((previous) => [
      job,
      ...previous.filter((item) => item.id !== job.id),
    ]);
  };

  const createJob = async (values: CronJobFormValues) => {
    try {
      saveRow(await octopCronApi.create(toOctopCreateBody(values)));
      message.success(t("cronJobs.createdSuccess"));
      return true;
    } catch (error) {
      console.error("Failed to create cron job", error);
      message.error(t("common.saveFailed"));
      return false;
    }
  };

  const updateJob = async (jobId: string, values: CronJobFormValues) => {
    try {
      saveRow(await octopCronApi.patch(jobId, toOctopPatchBody(values)));
      message.success(t("cronJobs.updatedSuccess"));
      return true;
    } catch (error) {
      console.error("Failed to update cron job", error);
      message.error(t("common.saveFailed"));
      return false;
    }
  };

  const deleteJob = async (jobId: string) => {
    try {
      await octopCronApi.delete(jobId);
      fetchGenRef.current++;
      setLoading(false);
      setRefreshing(false);
      setJobs((previous) => previous.filter((job) => job.id !== jobId));
      message.success(t("cronJobs.deletedSuccess"));
      return true;
    } catch (error) {
      console.error("Failed to delete cron job", error);
      message.error(t("cronJobs.deleteFailed"));
      return false;
    }
  };

  const toggleEnabled = async (job: CronJob) => {
    try {
      saveRow(await octopCronApi.patch(job.id, { enabled: !job.enabled }));
      message.success(t(job.enabled ? "common.disabled" : "common.enabled"));
      return true;
    } catch (error) {
      console.error("Failed to toggle cron job", error);
      message.error(t("cronJobs.operationFailed"));
      return false;
    }
  };

  const executeNow = async (jobId: string) => {
    try {
      await octopCronApi.runNow(jobId);
      await fetchJobs();
      message.success(t("cronJobs.triggeredSuccess"));
      return true;
    } catch (error) {
      console.error("Failed to execute cron job", error);
      message.error(t("cronJobs.executeFailed"));
      return false;
    }
  };

  return {
    jobs,
    loading,
    listRefreshing: refreshing,
    listStale: false,
    cronTimezone,
    createJob,
    updateJob,
    deleteJob,
    toggleEnabled,
    executeNow,
    refetchJobs: fetchJobs,
    jobToFormValues,
  };
}
