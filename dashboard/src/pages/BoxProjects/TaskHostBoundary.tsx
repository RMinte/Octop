import { useEffect, useState, type ReactNode } from "react";
import { Alert, Button, Spin } from "antd";
import { useTranslation } from "react-i18next";
import { TaskHostProvider } from "../../context/TaskHostContext";
import { bridgeApi } from "../../api/modules/bridge";
import { apiErrorMessage } from "../../utils/apiError";
import PageShell from "../../layouts/PageShell";
import TaskHostDirectory from "./TaskHostDirectory";

export default function TaskHostBoundary({
  hostID,
  children,
}: {
  hostID: string | null;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const [available, setAvailable] = useState(!hostID);
  const [identity, setIdentity] = useState(hostID ?? "local");
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!hostID) return;
    let active = true;
    const refresh = () => {
      void bridgeApi
        .taskCapabilities(hostID)
        .then((value) => {
          if (active) {
            const supported =
              value.available &&
              value.protocol_version === 3 &&
              value.capabilities.tasks &&
              value.capabilities.projects &&
              value.capabilities.task_ws;
            setIdentity(
              JSON.stringify(value.peer_identity ?? { host_id: hostID }),
            );
            setAvailable(supported);
            setError(supported ? "" : t("projects.tasks.hosts.unavailable"));
          }
        })
        .catch((error) => {
          if (active) {
            setAvailable(false);
            setError(apiErrorMessage(error, t("projects.tasks.failure"), t));
          }
        });
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => {
      active = false;
      window.removeEventListener("focus", refresh);
    };
  }, [hostID, version, t]);
  if (!available)
    return (
      <PageShell title={t("projects.title")}>
        <TaskHostDirectory hostID={hostID} />
        {error ? (
          <Alert
            type="error"
            message={error}
            action={
              <Button
                onClick={() => {
                  setError("");
                  setVersion((v) => v + 1);
                }}
              >
                {t("projects.refresh")}
              </Button>
            }
          />
        ) : (
          <Spin />
        )}
      </PageShell>
    );
  return (
    <TaskHostProvider key={identity} hostID={hostID} identity={identity}>
      {children}
    </TaskHostProvider>
  );
}
