import {
  createKnowledgeBasesApi,
  knowledgeBasesApi,
} from "../api/modules/knowledgeBases";
import { createContext, useContext, useMemo, type ReactNode } from "react";
import {
  createProjectsApi,
  createProjectTasksApi,
  projectsApi,
  projectTasksApi,
} from "../api/modules/projects";
import {
  createTaskWorkbenchApi,
  taskWorkbenchApi,
} from "../api/modules/taskWorkbench";
import {
  createTaskExpertMemoryApi,
  taskExpertMemoryApi,
} from "../api/modules/taskExpertMemory";
import { createTaskExtrasApi, taskExtrasApi } from "../api/modules/taskExtras";

const local = {
  hostID: null as string | null,
  identity: "local",
  get projectsApi() {
    return projectsApi;
  },
  get projectTasksApi() {
    return projectTasksApi;
  },
  get taskWorkbenchApi() {
    return taskWorkbenchApi;
  },
  get taskExpertMemoryApi() {
    return taskExpertMemoryApi;
  },
  get taskExtrasApi() {
    return taskExtrasApi;
  },
  get knowledgeBasesApi() {
    return knowledgeBasesApi;
  },
};
const Context = createContext(local);
export function TaskHostProvider({
  hostID,
  children,
  identity,
}: {
  hostID: string | null;
  children: ReactNode;
  identity?: string;
}) {
  const value = useMemo(
    () =>
      hostID
        ? {
            hostID,
            identity: identity ?? hostID,
            projectsApi: createProjectsApi(hostID),
            projectTasksApi: createProjectTasksApi(hostID),
            taskWorkbenchApi: createTaskWorkbenchApi(hostID),
            taskExpertMemoryApi: createTaskExpertMemoryApi(hostID),
            taskExtrasApi: createTaskExtrasApi(hostID),
            knowledgeBasesApi: createKnowledgeBasesApi(hostID),
          }
        : local,
    [hostID, identity],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export const useTaskHost = () => useContext(Context);
