import { Button, Switch, Tooltip } from "antd";
import { CheckCircle, CircleAlert, Plug } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  isACPJobPending,
  type ACPRunnerConfig,
  type ACPRunnerSetup,
  type ACPSetupJob,
} from "../../../../api/types/acp";
import { runnerIcon, runnerIntroKey, runnerLabelKey } from "../constants";
import { formatServerDateTime } from "../../../../utils/formatMessageTime";
import styles from "../../Channels/index.module.less";
import acpStyles from "../index.module.less";

interface ACPCardProps {
  runnerKey: string;
  config: ACPRunnerConfig;
  setup?: ACPRunnerSetup;
  job?: ACPSetupJob;
  timeZone: string;
  isHover: boolean;
  toggleLoading?: boolean;
  actionLoading?: "install" | "login" | "check" | "credentials" | "connect";
  busyElsewhere?: boolean;
  interactionDisabled?: boolean;
  onClick: () => void;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onToggleEnabled: (runnerKey: string, checked: boolean) => void;
  onInstall: (runnerKey: string) => void;
  onConnect: (runnerKey: string) => void;
  onCheck: (runnerKey: string) => void;
  onGuide: (runnerKey: string) => void;
}

export function ACPCard({
  runnerKey,
  config,
  setup,
  job,
  timeZone,
  isHover,
  toggleLoading,
  actionLoading,
  busyElsewhere = false,
  interactionDisabled = false,
  onClick,
  onMouseEnter,
  onMouseLeave,
  onToggleEnabled,
  onInstall,
  onConnect,
  onCheck,
  onGuide,
}: ACPCardProps) {
  const { t } = useTranslation();
  const labelKey = runnerLabelKey(runnerKey);
  const label = labelKey ? t(labelKey) : runnerKey;
  const introKey = runnerIntroKey(runnerKey);
  const intro = t(introKey, {
    command: config.command || t("acp.notSet"),
  });
  const ready = setup?.connection === "ready";
  const codex = runnerKey === "codex";
  const pending = isACPJobPending(job);
  const setupMessage =
    codex && setup?.connection === "auth_required"
      ? t(pending ? "acp.codexAuthorizationHint" : "acp.codexConnectHint")
      : setup?.message;
  const busy = Boolean(actionLoading || toggleLoading || busyElsewhere);
  const canEnable =
    !codex && ready && !interactionDisabled && !busy && !pending;
  const connectionKey =
    pending && (job?.kind === "login" || job?.kind === "connect")
      ? job.status === "waiting_for_user"
        ? "waitingAuthorization"
        : job.kind === "connect"
        ? "connectingCodex"
        : "authenticating"
      : setup?.connection === "auth_required"
      ? "authRequired"
      : setup?.connection === "error"
      ? "connectionError"
      : ready
      ? "connectionReady"
      : "unchecked";
  const showInstall = setup?.can_install && setup.installation !== "installed";
  const manualSetup = setup?.installation !== "installed" && !showInstall;
  const canAuthenticate =
    setup?.can_login || Boolean(setup?.credential_options?.length);
  const showLogin = setup?.connection === "auth_required" && canAuthenticate;
  const primaryAction = pending
    ? () => onGuide(runnerKey)
    : codex
    ? () => onConnect(runnerKey)
    : showInstall
    ? () => onInstall(runnerKey)
    : manualSetup || (setup?.connection === "auth_required" && !canAuthenticate)
    ? onClick
    : showLogin && !ready
    ? () => onGuide(runnerKey)
    : ready && !config.enabled
    ? () => onToggleEnabled(runnerKey, true)
    : () => onCheck(runnerKey);
  const primaryLabel = pending
    ? job?.kind === "connect"
      ? "openConnectProgress"
      : job?.kind === "install"
      ? "openInstallProgress"
      : "openLoginProgress"
    : codex
    ? ready && config.enabled
      ? "reconnectCodex"
      : "connectCodex"
    : showInstall
    ? "install"
    : manualSetup || (setup?.connection === "auth_required" && !canAuthenticate)
    ? "advancedConfig"
    : showLogin && !ready
    ? "login"
    : ready && !config.enabled
    ? "enableRunner"
    : "checkConnection";
  const installationLabel =
    pending && job?.kind === "connect"
      ? t(`acp.phase_${job.phase}`)
      : pending && job?.kind === "install"
      ? t("acp.installing")
      : setup
      ? t(`acp.installation_${setup.installation}`)
      : t("acp.setupUnknown");
  const statusClass = ready
    ? styles.statusConnected
    : setup?.connection === "error" || setup?.connection === "auth_required"
    ? acpStyles.statusWarning
    : styles.statusInactive;
  const Icon = ready
    ? CheckCircle
    : setup?.connection === "error"
    ? CircleAlert
    : Plug;

  return (
    <div
      role="group"
      aria-label={label}
      className={[
        styles.channelCard,
        config.enabled ? styles.enabled : styles.normal,
        isHover && !interactionDisabled ? styles.hover : "",
        acpStyles.runnerCard,
      ]
        .filter(Boolean)
        .join(" ")}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      <div className={styles.cardTop}>
        <img
          src={runnerIcon(runnerKey)}
          alt=""
          className={[
            styles.channelIcon,
            runnerKey === "pi" ? acpStyles.invertOnDark : "",
          ]
            .filter(Boolean)
            .join(" ")}
        />
        <span className={styles.cardTitle}>{label}</span>
        <Tooltip
          title={
            !config.enabled && !canEnable
              ? t(
                  interactionDisabled
                    ? "acp.outboundBlockedTooltip"
                    : codex
                    ? "acp.codexConnectHint"
                    : "acp.checkBeforeEnable",
                )
              : undefined
          }
        >
          <Switch
            size="small"
            aria-label={t("acp.runnerToggle", { name: label })}
            checked={config.enabled}
            loading={toggleLoading}
            disabled={busy || pending || (!config.enabled && !canEnable)}
            onChange={(checked) => onToggleEnabled(runnerKey, checked)}
          />
        </Tooltip>
      </div>
      {setup?.checked_at && !pending ? (
        <p className={acpStyles.checkedAt}>
          {t("acp.lastChecked", {
            time: formatServerDateTime(setup.checked_at / 1000, timeZone),
          })}
        </p>
      ) : null}
      <p className={styles.cardDescription}>{intro}</p>
      <div className={acpStyles.runnerStatus}>
        <span className={acpStyles.installationStatus}>
          {installationLabel}
        </span>
        <span className={`${styles.statusBadge} ${statusClass}`}>
          <Icon size={14} />
          {config.enabled
            ? t("acp.enabledStatus", { state: t(`acp.${connectionKey}`) })
            : t(`acp.${connectionKey}`)}
        </span>
      </div>
      {setupMessage ? (
        <p className={acpStyles.setupMessage}>{setupMessage}</p>
      ) : null}
      <div className={acpStyles.cardActions}>
        {primaryLabel !== "advancedConfig" ? (
          <Button
            type="primary"
            size="small"
            loading={Boolean(actionLoading)}
            disabled={!setup || interactionDisabled || busy}
            onClick={primaryAction}
          >
            {t(
              `acp.${
                actionLoading === "connect"
                  ? "connectingCodex"
                  : actionLoading === "install"
                  ? "installing"
                  : actionLoading === "check"
                  ? "checking"
                  : actionLoading === "login"
                  ? "authenticating"
                  : actionLoading === "credentials"
                  ? "savingCredentials"
                  : primaryLabel
              }`,
            )}
          </Button>
        ) : null}
        {!codex &&
        !pending &&
        (showLogin || manualSetup || (ready && !config.enabled)) ? (
          <Button
            size="small"
            disabled={interactionDisabled || busy}
            onClick={() => onCheck(runnerKey)}
          >
            {t("acp.checkConnection")}
          </Button>
        ) : null}
        {!codex &&
        !pending &&
        canAuthenticate &&
        setup?.installation === "installed" &&
        setup.connection !== "auth_required" ? (
          <Button
            type="link"
            size="small"
            disabled={interactionDisabled || busy}
            onClick={() => onGuide(runnerKey)}
          >
            {t("acp.login")}
          </Button>
        ) : null}
        <Button
          type="link"
          size="small"
          disabled={interactionDisabled || busy || pending}
          onClick={onClick}
        >
          {t("acp.advancedConfig")}
        </Button>
      </div>
    </div>
  );
}
