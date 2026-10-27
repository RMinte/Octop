import type {
  EngineCapabilities,
  EngineCatalogEntry,
  EngineSelection,
  TaskExecutionOptions,
} from "../../api/modules/projects";

export const OCTOP_ENGINE: EngineSelection = {
  engine_id: "octop",
  connection_id: null,
  model_id: null,
};
const NATIVE_CAPABILITIES: EngineCapabilities = {
  action_modes: ["ask", "plan", "craft"],
  images: true,
  permissions: true,
  native_resume: true,
  pause: true,
  cancel: true,
  skills: true,
  mcp: true,
  plugins: true,
  subagents: true,
  knowledge: true,
  memory: true,
  compact: true,
  model_selection: true,
};
const ACP_CAPABILITIES: EngineCapabilities = {
  action_modes: ["craft"],
  images: false,
  permissions: true,
  native_resume: false,
  pause: false,
  cancel: true,
  skills: false,
  mcp: false,
  plugins: false,
  subagents: false,
  knowledge: false,
  memory: false,
  compact: false,
  model_selection: false,
};
export function engineKey(engine: EngineSelection): string {
  return JSON.stringify([engine.engine_id, engine.connection_id]);
}
export function engineEntry(
  engine: EngineSelection,
  catalog?: EngineCatalogEntry[],
): EngineCatalogEntry | undefined {
  return catalog?.find(
    (entry) => engineKey(entry.engine) === engineKey(engine),
  );
}
export function engineCapabilities(
  engine: EngineSelection,
  catalog?: EngineCatalogEntry[],
): EngineCapabilities {
  return (
    engineEntry(engine, catalog)?.capabilities ??
    (engine.engine_id === "octop" ? NATIVE_CAPABILITIES : ACP_CAPABILITIES)
  );
}
export function engineLabel(engine: EngineSelection): string {
  return engine.engine_id === "octop" ? "Octop" : engine.connection_id || "ACP";
}
/** Octop model routing stays in the draft when switching; ACP receives its own model id. */
export function taskOptionsForEngine(
  options: TaskExecutionOptions,
  engine: EngineSelection,
): TaskExecutionOptions {
  if (engine.engine_id === "octop") return options;
  const {
    model_ref: _model,
    reasoning_mode: _reasoning,
    reasoning_effort: _effort,
    routing: _routing,
    ...rest
  } = options;
  return rest;
}
