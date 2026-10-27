export type ACPToolParseMode = "call_title" | "update_detail" | "call_detail";

export const ACP_DEFAULT_STDIO_BUFFER_LIMIT_BYTES = 50 * 1024 * 1024;

export interface ACPRunnerConfig {
  enabled: boolean;
  command: string;
  args: string[];
  env: Record<string, string>;
  trusted: boolean;
  tool_parse_mode: ACPToolParseMode;
  stdio_buffer_limit_bytes: number;
}

export interface ACPConfig {
  tool_enabled: boolean;
  runners: Record<string, ACPRunnerConfig>;
}

export interface ACPRunnerSetup {
  runner: string;
  installation: "not_installed" | "installed" | "missing_dependency" | "manual";
  connection: "unchecked" | "ready" | "auth_required" | "error";
  enabled: boolean;
  can_install: boolean;
  can_login: boolean;
  credential_options: Array<{ id: string; label: string }>;
  install_command: string | null;
  login_command: string | null;
  docs_url: string | null;
  message: string | null;
  checked_at: number | null;
}

export interface ACPSetupView {
  host: string;
  runners: Record<string, ACPRunnerSetup>;
  active_jobs: Record<string, ACPSetupJob>;
}

export interface ACPSetupJob {
  job_id: string;
  runner: string;
  kind: "install" | "login";
  status: "running" | "waiting_for_user" | "succeeded" | "failed" | "cancelled";
  phase: string;
  output: string;
  message: string | null;
  authorization: {
    url: string | null;
    user_code: string | null;
    expires_at: number | null;
  } | null;
  input_mode: "none" | "terminal";
  setup: ACPRunnerSetup | null;
}

export function isACPJobPending(job?: ACPSetupJob): boolean {
  return job?.status === "running" || job?.status === "waiting_for_user";
}

export function parseArgsText(value: unknown): string[] {
  return String(value || "")
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function parseEnvText(value: unknown): Record<string, string> {
  return String(value || "")
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean)
    .reduce<Record<string, string>>((acc, line) => {
      const index = line.indexOf("=");
      if (index >= 0) {
        const key = line.slice(0, index).trim();
        const envValue = line.slice(index + 1).trim();
        if (key) acc[key] = envValue;
      }
      return acc;
    }, {});
}

export function stringifyArgs(args: string[] = []): string {
  return args.join("\n");
}

export function stringifyEnv(env: Record<string, string> = {}): string {
  return Object.entries(env)
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
}
