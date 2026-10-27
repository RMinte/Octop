import { Button, Select, Space, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type {
  EngineCatalogEntry,
  EngineSelection,
} from "../../api/modules/projects";
import {
  engineEntry,
  engineKey,
  engineLabel,
  OCTOP_ENGINE,
} from "./taskEngine";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { canAccessKeys } from "../../utils/permissions";

export default function TaskEngineSelect({
  value,
  catalog,
  disabled,
  loading,
  hostID,
  onChange,
  onRefresh,
  branch = false,
}: {
  value: EngineSelection;
  catalog?: EngineCatalogEntry[];
  disabled: boolean;
  loading?: boolean;
  hostID: string | null;
  onChange: (engine: EngineSelection) => void;
  onRefresh?: () => void;
  branch?: boolean;
}) {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const canManage = canAccessKeys(user, "admin");
  const current = engineEntry(value, catalog);
  const unavailable = catalog
    ? !current?.available
    : value.engine_id !== "octop";
  const entries = catalog ?? [
    {
      engine: OCTOP_ENGINE,
      label: "Octop",
      available: true,
      unavailable_reason: null,
    },
  ];
  return (
    <Space size={4} wrap>
      <Tooltip title={t("projects.tasks.engineSelectionHint")}>
        <Select
          aria-label={t(
            branch ? "projects.tasks.branchEngine" : "projects.tasks.engine",
          )}
          size="small"
          style={{ minWidth: 145 }}
          loading={loading}
          disabled={disabled}
          value={engineKey(value)}
          options={[
            ...entries.map((entry) => ({
              value: engineKey(entry.engine),
              label: `${entry.label}${
                !entry.available
                  ? ` · ${t(
                      `projects.tasks.engineReasons.${entry.unavailable_reason}`,
                      { defaultValue: t("projects.tasks.engineUnavailable") },
                    )}`
                  : ""
              }`,
              disabled: !entry.available,
            })),
            ...(!current &&
            !entries.some(
              (entry) => engineKey(entry.engine) === engineKey(value),
            )
              ? [
                  {
                    value: engineKey(value),
                    label: `${engineLabel(value)} · ${t(
                      "projects.tasks.engineUnavailable",
                    )}`,
                    disabled: true,
                  },
                ]
              : []),
          ]}
          onOpenChange={(open) => {
            if (open) onRefresh?.();
          }}
          onChange={(key) => {
            const entry = entries.find(
              (item) => engineKey(item.engine) === key,
            );
            if (entry?.available) onChange({ ...entry.engine, model_id: null });
          }}
        />
      </Tooltip>
      {unavailable ? (
        <Tooltip
          title={
            hostID
              ? t("projects.tasks.remoteEngineManagement")
              : t("projects.tasks.engineSetupHint")
          }
        >
          <Typography.Text type="warning">
            {t("projects.tasks.engineUnavailable")}
          </Typography.Text>
        </Tooltip>
      ) : null}
      {!branch && !hostID && canManage ? (
        <Button
          size="small"
          type="link"
          href="/acp"
          target="_blank"
          rel="noopener noreferrer"
        >
          {t("projects.tasks.manageEngines")}
        </Button>
      ) : null}
      {!branch && !hostID && !canManage ? (
        <Typography.Text type="secondary">
          {t("projects.tasks.engineManagementAdminOnly")}
        </Typography.Text>
      ) : null}
      {!branch && hostID ? (
        <Typography.Text type="secondary">
          {t("projects.tasks.remoteEngineManagement")}
        </Typography.Text>
      ) : null}
    </Space>
  );
}
