import { request } from "../request";

export type TaskUsageUserFilter = number | "all" | null;
export type TaskTokenField =
  | "input_tokens"
  | "output_tokens"
  | "total_tokens"
  | "uncached_input_tokens"
  | "cache_read_tokens"
  | "cache_write_tokens"
  | "reasoning_tokens";
export type TaskTokenTotals = Record<TaskTokenField, number | null>;
export type TaskTokenCounts = Record<TaskTokenField, number>;
export type TaskTokenCoverage = Record<
  TaskTokenField,
  "recorded" | "partial" | "not_collected" | "no_data"
>;

export interface ReferenceCostAmounts {
  amount_usd: string | null;
  known_amount_usd: string | null;
  lower_bound_usd: string | null;
  upper_bound_usd: string | null;
  status: "estimated" | "estimated_range" | "partial" | "unknown" | "no_data";
  missing_fields: string[];
  assumptions: string[];
}

export interface TaskCallUsage {
  calls: number;
  statuses: Record<string, number>;
  tokens: TaskTokenTotals;
  missing_calls: TaskTokenCounts;
  derived_calls: TaskTokenCounts;
  coverage: TaskTokenCoverage;
  attribution_counts: Record<string, number>;
  reference_cost: ReferenceCostAmounts & {
    estimated_calls: number;
    estimated_range_calls: number;
    partial_calls: number;
    unknown_calls: number;
  };
}

export type RetainedReferenceCost = ReferenceCostAmounts & {
  estimated_records: number;
  estimated_range_records: number;
  partial_records: number;
  unknown_records: number;
};

export interface RetainedSourceTotals {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  usage_records: number;
  source_basis: "configuration_inference" | "unattributed" | "no_data";
  reference_cost: RetainedReferenceCost;
}

export interface RetainedTaskUsageTotals {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  turns: number | null;
  usage_records: number;
  coverage: "recorded" | "no_data";
  source_attribution: "inferred" | "partial" | "unknown" | "no_data";
  sources: { box: RetainedSourceTotals; third_party: RetainedSourceTotals };
  unattributed_usage: RetainedSourceTotals;
  reference_cost: RetainedReferenceCost;
  measurement_unit: "retained_task_journal";
  cannot_add_to_call_usage: true;
}

export interface TaskUsageTotals {
  call_usage: TaskCallUsage;
  sources: { box: TaskCallUsage; third_party: TaskCallUsage };
  retained_task_usage: RetainedTaskUsageTotals;
}

export interface TaskUsageItem extends TaskUsageTotals {
  task_id: string;
  title: string;
  project_id: string;
  project_name: string;
  last_usage_at: number;
}

export interface ReferencePricingRates {
  input: string;
  cache_read: string;
  cache_write_30m: string;
  cache_write_12h: string;
  output: string;
}

export interface TaskUsagePage {
  window: string;
  range_start: number;
  range_end: number;
  timezone: string;
  generated_at: number;
  account_scope: "user" | "all_users";
  user_id: number | null;
  measurement_unit: "logical_model_invocation";
  recording_scope: "retained_records_only";
  history_complete: false;
  physical_attempts_available: false;
  capabilities: {
    source_breakdown: true;
    reference_cost: true;
    historical_source_inference: true;
    actual_cost: false;
    execution_location: false;
    savings: false;
    budget: false;
  };
  pricing: {
    model: string;
    version: string;
    checked_at: string;
    currency: string;
    basis: "openai_reference_estimate";
    service_tier: "standard";
    rate_unit: "USD_per_million_tokens";
    long_context_threshold: number;
    short_context: ReferencePricingRates;
    long_context: ReferencePricingRates;
    source_urls: string[];
    assumptions: string[];
  };
  total_tasks: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  totals: TaskUsageTotals;
  unassigned_call_usage: TaskCallUsage;
  unassigned_sources: { box: TaskCallUsage; third_party: TaskCallUsage };
  items: TaskUsageItem[];
}

export interface TaskUsageQuery {
  window: string;
  userFilter?: TaskUsageUserFilter;
  task_id?: string;
  project_id?: string;
  offset?: number;
  limit?: number;
}

export const taskUsageApi = {
  summary: ({ userFilter = null, ...query }: TaskUsageQuery) => {
    const params = new URLSearchParams({ window: query.window });
    for (const key of ["task_id", "project_id", "offset", "limit"] as const) {
      if (query[key] !== undefined) params.set(key, String(query[key]));
    }
    if (typeof userFilter === "number") {
      params.set("user_id", String(userFilter));
    }
    const prefix = userFilter === null ? "/usage" : "/admin/usage";
    return request<TaskUsagePage>(`${prefix}/task-summary?${params}`, {
      cache: "no-store",
    });
  },
};
