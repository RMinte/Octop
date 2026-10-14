import { request } from "../request";

const PREFIX = "/agentcomputer/v1";

export type BoxSource = { connectionId: string | null };

function resource(source: BoxSource, path: string): string {
  const prefix = source.connectionId
    ? `/bridge/connections/${encodeURIComponent(source.connectionId)}${PREFIX}`
    : PREFIX;
  return `${prefix}${path}`;
}

function withCursor(path: string, cursor?: string | null): string {
  return cursor ? `${path}?cursor=${encodeURIComponent(cursor)}` : path;
}

export interface CapabilityStatus {
  supported: boolean;
  available: boolean;
  reasonCode: string | null;
}

export interface BoxCapabilities {
  boxID: string;
  restoreEpoch: number;
  textModelRef: string | null;
  supportedPermissionModes: string[];
  capabilities: {
    projects: CapabilityStatus;
    tasks: CapabilityStatus;
    runs: CapabilityStatus;
  };
}

export interface BoxProject {
  projectID: string;
  name: string;
  projectRevision: number;
  preparationState: "pending" | "preparing" | "ready" | "failed";
  reasonCode: string | null;
  executionReady: boolean;
}

export interface BoxTask {
  taskID: string;
  projectID: string;
  title: string;
  taskRevision: number;
  preparationState: "pending" | "preparing" | "ready" | "failed";
  executionReady: boolean;
}

export type BoxRunState =
  | "queued"
  | "running"
  | "waitingApproval"
  | "waitingInput"
  | "succeeded"
  | "failed"
  | "stopped"
  | "interrupted";

export interface BoxRun {
  runID: string;
  taskID: string;
  projectID: string;
  state: BoxRunState;
  reconciliationRequired: boolean;
  updatedAt: string;
}

export interface BoxTurn {
  turnID: string;
  prompt: string;
  run: BoxRun;
  result: {
    runID: string;
    state: BoxRunState;
    text: string | null;
  };
}

export interface BoxPage<T> {
  items: T[];
  nextCursor: string | null;
}

export interface BoxTaskListItem {
  task: BoxTask;
  latestRun: BoxRun | null;
}

export interface BoxProjectDocumentList {
  items: string[];
}

export interface BoxProjectDocument {
  filename: string;
  content: string;
}

export interface BoxCommandReceipt {
  commandID: string;
  accepted: boolean;
  operation?: "project.create" | "task.create";
  projectID?: string;
  taskID?: string;
  turnID?: string;
  runID?: string;
}

export interface CreateProjectCommand {
  commandID: string;
  idempotencyKey: string;
  expectedRestoreEpoch: number;
  projectID: string;
  name: string;
  configuration: {
    schemaVersion: 1;
    instructions: string;
    modelRef: string;
  };
}

export interface CreateTaskCommand {
  commandID: string;
  idempotencyKey: string;
  expectedRestoreEpoch: number;
  expectedProjectRevision: number;
  projectID: string;
  taskID: string;
  title: string;
}

export interface SendTurnCommand {
  commandID: string;
  idempotencyKey: string;
  expectedRestoreEpoch: number;
  contextMode: "newMessage";
  userMessageID: string;
  expectedProjectRevision: number;
  prompt: string;
  inputArtifactIDs: [];
  selectedSkillIDs: [];
  permissionMode: "askForApproval";
}

export const agentComputerApi = {
  capabilities: (source: BoxSource) =>
    request<BoxCapabilities>(resource(source, "/capabilities")),
  projects: (source: BoxSource, cursor?: string | null) =>
    request<BoxPage<BoxProject>>(
      withCursor(resource(source, "/projects"), cursor),
    ),
  project: (source: BoxSource, projectID: string) =>
    request<BoxProject>(
      resource(source, `/projects/${encodeURIComponent(projectID)}`),
    ),
  listProjectDocuments: (source: BoxSource, projectID: string) =>
    request<BoxProjectDocumentList>(
      resource(source, `/projects/${encodeURIComponent(projectID)}/documents`),
    ),
  getProjectDocument: (
    source: BoxSource,
    projectID: string,
    filename: string,
  ) =>
    request<BoxProjectDocument>(
      resource(
        source,
        `/projects/${encodeURIComponent(
          projectID,
        )}/documents/${encodeURIComponent(filename)}`,
      ),
    ),
  createProjectDocument: (
    source: BoxSource,
    projectID: string,
    filename: string,
    content: string,
  ) =>
    request<BoxProjectDocument>(
      resource(
        source,
        `/projects/${encodeURIComponent(
          projectID,
        )}/documents/${encodeURIComponent(filename)}`,
      ),
      { method: "PUT", body: JSON.stringify({ content }) },
    ),
  createProject: (source: BoxSource, command: CreateProjectCommand) =>
    request<BoxCommandReceipt>(resource(source, "/projects"), {
      method: "POST",
      body: JSON.stringify(command),
    }),
  tasks: (source: BoxSource, projectID: string, cursor?: string | null) =>
    request<BoxPage<BoxTaskListItem>>(
      withCursor(
        resource(source, `/projects/${encodeURIComponent(projectID)}/tasks`),
        cursor,
      ),
    ),
  task: (source: BoxSource, taskID: string) =>
    request<BoxTask>(resource(source, `/tasks/${encodeURIComponent(taskID)}`)),
  createTask: (
    source: BoxSource,
    projectID: string,
    command: CreateTaskCommand,
  ) =>
    request<BoxCommandReceipt>(
      resource(source, `/projects/${encodeURIComponent(projectID)}/tasks`),
      { method: "POST", body: JSON.stringify(command) },
    ),
  turns: (source: BoxSource, taskID: string, cursor?: string | null) =>
    request<BoxPage<BoxTurn>>(
      withCursor(
        resource(source, `/tasks/${encodeURIComponent(taskID)}/turns`),
        cursor,
      ),
    ),
  sendTurn: (source: BoxSource, taskID: string, command: SendTurnCommand) =>
    request<BoxCommandReceipt>(
      resource(source, `/tasks/${encodeURIComponent(taskID)}/turns`),
      {
        method: "POST",
        body: JSON.stringify(command),
      },
    ),
  command: (source: BoxSource, commandID: string) =>
    request<BoxCommandReceipt>(
      resource(source, `/commands/${encodeURIComponent(commandID)}`),
    ),
};
