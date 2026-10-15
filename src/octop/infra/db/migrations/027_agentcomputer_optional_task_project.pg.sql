-- Schema v27: a Task may own its execution context without a project.
-- Execution journals gain nullable project IDs and mandatory Task/owner lineage.
-- Their runtime API remains closed until independent execution proofs are ready.

ALTER TABLE ac_tasks ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE ac_tasks ADD COLUMN IF NOT EXISTS standalone_box_id TEXT;
ALTER TABLE ac_tasks ADD COLUMN IF NOT EXISTS standalone_workspace_id TEXT;
ALTER TABLE ac_tasks ADD COLUMN IF NOT EXISTS standalone_agent_id TEXT;
ALTER TABLE ac_tasks ADD COLUMN IF NOT EXISTS standalone_config_json TEXT;
ALTER TABLE ac_tasks ADD COLUMN IF NOT EXISTS standalone_config_digest TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ac_tasks_standalone_workspace
  ON ac_tasks(standalone_workspace_id) WHERE standalone_workspace_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ac_tasks_standalone_agent
  ON ac_tasks(standalone_agent_id) WHERE standalone_agent_id IS NOT NULL;
ALTER TABLE ac_tasks DROP CONSTRAINT IF EXISTS ac_tasks_scope_shape;
ALTER TABLE ac_tasks ADD CONSTRAINT ac_tasks_scope_shape CHECK (
  (project_id IS NOT NULL AND standalone_box_id IS NULL
    AND standalone_workspace_id IS NULL AND standalone_agent_id IS NULL
    AND standalone_config_json IS NULL AND standalone_config_digest IS NULL)
  OR (project_id IS NULL AND standalone_box_id IS NOT NULL
    AND standalone_workspace_id IS NOT NULL AND standalone_agent_id IS NOT NULL
    AND standalone_config_json IS NOT NULL AND standalone_config_digest IS NOT NULL)
);

ALTER TABLE agents ADD COLUMN IF NOT EXISTS managed_task_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_managed_task
  ON agents(managed_task_id) WHERE managed_task_id IS NOT NULL;
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_managed_project_pair;
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_managed_task_fk;
ALTER TABLE agents ADD CONSTRAINT agents_managed_task_fk
  FOREIGN KEY (managed_task_id) REFERENCES ac_tasks(task_id);
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_managed_scope_pair;
ALTER TABLE agents ADD CONSTRAINT agents_managed_scope_pair CHECK (
  (managed_by IS NULL AND managed_project_id IS NULL AND managed_task_id IS NULL)
  OR (managed_by IS NOT NULL AND managed_by = 'agentcomputer' AND (
    (managed_project_id IS NOT NULL AND managed_task_id IS NULL)
    OR (managed_project_id IS NULL AND managed_task_id IS NOT NULL)
  ))
);

ALTER TABLE ac_turns ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE ac_turns DROP CONSTRAINT IF EXISTS ac_turns_task_owner_fk;
ALTER TABLE ac_turns ADD CONSTRAINT ac_turns_task_owner_fk
  FOREIGN KEY (task_id, owner_id) REFERENCES ac_tasks(task_id, owner_id);

ALTER TABLE ac_runs ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE ac_runs DROP CONSTRAINT IF EXISTS ac_runs_task_owner_fk;
ALTER TABLE ac_runs ADD CONSTRAINT ac_runs_task_owner_fk
  FOREIGN KEY (task_id, owner_id) REFERENCES ac_tasks(task_id, owner_id);
ALTER TABLE ac_runs DROP CONSTRAINT IF EXISTS ac_runs_turn_task_owner_fk;
ALTER TABLE ac_runs ADD CONSTRAINT ac_runs_turn_task_owner_fk
  FOREIGN KEY (turn_id, task_id, owner_id) REFERENCES ac_turns(turn_id, task_id, owner_id);

ALTER TABLE ac_run_dispatches ALTER COLUMN project_id DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ac_run_dispatches_effect_binding_task
  ON ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id);

ALTER TABLE ac_tool_effects ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE ac_tool_effects DROP CONSTRAINT IF EXISTS ac_tool_effects_dispatch_task_owner_fk;
ALTER TABLE ac_tool_effects ADD CONSTRAINT ac_tool_effects_dispatch_task_owner_fk
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id);

ALTER TABLE ac_run_pauses ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE ac_run_pauses DROP CONSTRAINT IF EXISTS ac_run_pauses_dispatch_task_owner_fk;
ALTER TABLE ac_run_pauses ADD CONSTRAINT ac_run_pauses_dispatch_task_owner_fk
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id);

ALTER TABLE ac_run_cancellations ALTER COLUMN project_id DROP NOT NULL;

ALTER TABLE ac_run_terminals ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE ac_run_terminals DROP CONSTRAINT IF EXISTS ac_run_terminals_dispatch_task_owner_fk;
ALTER TABLE ac_run_terminals ADD CONSTRAINT ac_run_terminals_dispatch_task_owner_fk
  FOREIGN KEY (dispatch_id, run_id, turn_id, task_id, owner_id,
    restore_epoch, execution_epoch, claimant_id, engine_request_id)
    REFERENCES ac_run_dispatches(dispatch_id, run_id, turn_id, task_id, owner_id,
      restore_epoch, execution_epoch, claimant_id, engine_request_id);

UPDATE _schema_version SET version = 27;
