import { useTaskHost } from "../context/TaskHostContext";
import { useEffect, useState } from "react";
import { type TaskPresentationSettings } from "../api/modules/projects";

export const DEFAULT_TASK_PRESENTATION_SETTINGS: TaskPresentationSettings = {
  action_timeline_enabled: true,
  step_panel_enabled: true,
};

const settingsChanged = "octop:task-presentation-settings-changed";

export function invalidateTaskPresentationSettings() {
  window.dispatchEvent(new Event(settingsChanged));
}

/** One page owns these preferences and passes them to all loaded turns. */
export function useTaskPresentationSettings(): TaskPresentationSettings {
  const { projectTasksApi } = useTaskHost();
  const [settings, setSettings] = useState(DEFAULT_TASK_PRESENTATION_SETTINGS);

  useEffect(() => {
    let active = true;
    let revision = 0;
    const refresh = () => {
      const requestRevision = ++revision;
      void projectTasksApi.settings().then(
        (next) => {
          if (active && revision === requestRevision) setSettings(next);
        },
        () => {
          // Keep the last confirmed preferences; focus or re-entry retries.
        },
      );
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };
    refresh();
    window.addEventListener("focus", refresh);
    window.addEventListener(settingsChanged, refresh);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      active = false;
      window.removeEventListener("focus", refresh);
      window.removeEventListener(settingsChanged, refresh);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [projectTasksApi]);

  return settings;
}
