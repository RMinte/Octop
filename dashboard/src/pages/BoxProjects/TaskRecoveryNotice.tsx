import { Alert, Button, Collapse, Descriptions, Space, Typography } from "antd";
import { Play } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { TaskTurn } from "../../api/modules/projects";

export default function TaskRecoveryNotice({
  turn,
  onResume,
  disabled = false,
}: {
  turn: TaskTurn;
  onResume?: () => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const recovery = turn.recovery;
  if (!recovery || !["interrupted", "failed"].includes(turn.status))
    return null;
  const resumable =
    turn.status === "interrupted" &&
    !turn.history_only &&
    recovery.can_resume &&
    !recovery.needs_reconciliation;
  const title = turn.history_only
    ? "historyOnly"
    : recovery.needs_reconciliation
    ? "reconcile"
    : resumable
    ? "resumable"
    : "unavailable";
  return (
    <Space direction="vertical" style={{ width: "100%", marginBottom: 10 }}>
      <Alert
        showIcon
        type={recovery.needs_reconciliation ? "warning" : "info"}
        message={t(`projects.recovery.${title}`)}
        description={t(
          `projects.recovery.${
            recovery.needs_reconciliation
              ? "reconcileHint"
              : resumable
              ? "resumableHint"
              : "unavailableHint"
          }`,
        )}
        action={
          resumable ? (
            <Button
              size="small"
              icon={<Play size={14} />}
              disabled={disabled || !onResume}
              onClick={onResume}
            >
              {t("projects.recovery.resume")}
            </Button>
          ) : undefined
        }
      />
      <Collapse
        size="small"
        items={[
          {
            key: "diagnostic",
            label: t("projects.recovery.details"),
            children: (
              <Descriptions
                size="small"
                column={1}
                items={[
                  {
                    key: "phase",
                    label: t("projects.recovery.phase"),
                    children: recovery.phase,
                  },
                  {
                    key: "reason",
                    label: t("projects.recovery.reason"),
                    children: (
                      <Typography.Text code>
                        {recovery.reason_code}
                      </Typography.Text>
                    ),
                  },
                  {
                    key: "diagnostic",
                    label: t("projects.recovery.diagnosticID"),
                    children: (
                      <Typography.Text code copyable>
                        {recovery.diagnostic_id}
                      </Typography.Text>
                    ),
                  },
                  {
                    key: "cause",
                    label: t("projects.recovery.cause"),
                    children: recovery.cause_type,
                  },
                  ...(recovery.resource_type || recovery.resource_id
                    ? [
                        {
                          key: "resource",
                          label: t("projects.recovery.resource"),
                          children: [
                            recovery.resource_type,
                            recovery.resource_id,
                          ]
                            .filter(Boolean)
                            .join(" · "),
                        },
                      ]
                    : []),
                  ...(recovery.status_code != null
                    ? [
                        {
                          key: "status",
                          label: t("projects.recovery.statusCode"),
                          children: recovery.status_code,
                        },
                      ]
                    : []),
                ]}
              />
            ),
          },
        ]}
      />
    </Space>
  );
}
