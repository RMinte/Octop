import { request } from "../request";
import type {
  OctopCronRow,
  OctopCronCreateBody,
  OctopCronPatchBody,
} from "../types";

export interface OctopCronSettings {
  timezone: string;
}

export interface CronTaskExamples {
  zh?: string[];
  en?: string[];
}

export const octopCronApi = {
  settings: () => request<OctopCronSettings>("/settings/timezone"),

  list: () => request<OctopCronRow[]>("/cron", { cache: "no-store" }),

  create: (body: OctopCronCreateBody) =>
    request<OctopCronRow>("/cron", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  get: (cronId: string) =>
    request<OctopCronRow>(`/cron/${encodeURIComponent(cronId)}`),

  patch: (cronId: string, body: OctopCronPatchBody) =>
    request<OctopCronRow>(`/cron/${encodeURIComponent(cronId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  delete: (cronId: string) =>
    request<void>(`/cron/${encodeURIComponent(cronId)}`, { method: "DELETE" }),

  runNow: (cronId: string) =>
    request<void>(`/cron/${encodeURIComponent(cronId)}/run-now`, {
      method: "POST",
    }),
};
