import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, App, Button, Empty, Form, Switch } from "antd";

import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import { CardSkeleton } from "../../../components/Skeleton";
import { acpApi } from "../../../api/modules/acp";
import {
  ACP_DEFAULT_STDIO_BUFFER_LIMIT_BYTES,
  type ACPRunnerConfig,
  type ACPRunnerSetup,
  type ACPSetupJob,
  isACPJobPending,
} from "../../../api/types/acp";
import { useAgent } from "../../../context/AgentContext";
import { blocksAcpOutboundFromConfig } from "../../Experts/components/agentBackendForm";
import { ACPCard } from "./components/ACPCard";
import { ACPSetupGuide } from "./components/ACPSetupGuide";
import { isBridgeAgentId } from "../../../utils/remoteExpert";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import {
  ACPDrawer,
  formValuesToRunner,
  runnerToFormValues,
} from "./components/ACPDrawer";
import { BUILTIN_RUNNER_ORDER, isBuiltinRunner } from "./constants";
import channelStyles from "../Channels/index.module.less";
import styles from "./index.module.less";

const EMPTY_RUNNERS: Record<string, ACPRunnerConfig> = {};

async function readHostState(runnersScopeId?: string | null) {
  const [config, setup] = await Promise.allSettled([
    acpApi.getGlobalRunners(runnersScopeId),
    acpApi.getSetup(),
  ]);
  // A connect commit can land between these reads. Reconcile once when no job
  // remains to poll; repeated changes must not create a background retry loop.
  if (
    config.status === "fulfilled" &&
    setup.status === "fulfilled" &&
    config.value.runners.codex &&
    setup.value.runners.codex &&
    config.value.runners.codex.enabled !== setup.value.runners.codex.enabled &&
    !isACPJobPending(setup.value.active_jobs?.codex)
  ) {
    const [latestConfig] = await Promise.allSettled([
      acpApi.getGlobalRunners(runnersScopeId),
    ]);
    return [latestConfig, setup] as const;
  }
  return [config, setup] as const;
}

interface ACPPanelProps {
  /** When set (Experts tools drawer), use this agent instead of the chat selection. */
  agentId?: string | null;
}

/** ACP runners manager — shared by `/acp` and Personalization → Tools. */
export function ACPPanel({ agentId: agentIdProp }: ACPPanelProps = {}) {
  const { t } = useTranslation();
  const { modal, message } = App.useApp();
  const timeZone = useServerTimezone();
  const { agents } = useAgent();
  const expertScoped = agentIdProp !== undefined;
  const activeAgentId = agentIdProp ?? null;
  /** Local expert drawers retain their ownership scope; remote setup is gated below. */
  const runnersScopeId = agentIdProp;
  const [runners, setRunners] =
    useState<Record<string, ACPRunnerConfig>>(EMPTY_RUNNERS);
  const [toolEnabled, setToolEnabled] = useState(false);
  const [runnersLoading, setRunnersLoading] = useState(true);
  const [toolLoading, setToolLoading] = useState(false);
  const [toolToggleLoading, setToolToggleLoading] = useState(false);
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const [toggleLoadingKey, setToggleLoadingKey] = useState<string | null>(null);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [isCreateMode, setIsCreateMode] = useState(false);
  const [saving, setSaving] = useState(false);
  const [setup, setSetup] = useState<Record<string, ACPRunnerSetup>>({});
  const [host, setHost] = useState("");
  const [setupFailed, setSetupFailed] = useState(false);
  const [setupReload, setSetupReload] = useState(0);
  const [action, setAction] = useState<{
    key: string;
    type: "install" | "login" | "check" | "credentials" | "connect";
  } | null>(null);
  const [jobs, setJobs] = useState<Record<string, ACPSetupJob>>({});
  const [inputLoading, setInputLoading] = useState(false);
  const [cancelLoading, setCancelLoading] = useState(false);
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const [guideKey, setGuideKey] = useState<string | null>(null);
  const [form] = Form.useForm();
  const activeAgentIdRef = useRef(activeAgentId);
  const completedJobs = useRef(new Set<string>());
  const mounted = useRef(true);

  const activeAgent = useMemo(
    () => agents.find((row) => row.agent_id === activeAgentId) ?? null,
    [agents, activeAgentId],
  );
  const remoteDrawer =
    expertScoped &&
    (isBridgeAgentId(agentIdProp) || Boolean(activeAgent?.bridge));
  const outboundBlocked =
    expertScoped &&
    !activeAgent?.bridge &&
    blocksAcpOutboundFromConfig(activeAgent?.config ?? null);
  const setupScope = remoteDrawer ? "remote" : runnersScopeId ?? "local";
  const setupScopeRef = useRef(setupScope);
  const pendingJobs = Object.values(jobs).filter(isACPJobPending);
  const pendingJobKey = pendingJobs
    .map((job) => job.job_id)
    .sort()
    .join(",");
  const hostBusy = pendingJobs.length > 0;
  // Expert drawers retain their sandbox restrictions. Standalone host setup is
  // independent of the selected expert; its acp_runner toggle stays separate.

  useEffect(() => {
    activeAgentIdRef.current = activeAgentId;
  }, [activeAgentId]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    setupScopeRef.current = setupScope;
    setGuideKey(null);
    setDrawerOpen(false);
    setJobs({});
    setActionErrors({});
    completedJobs.current.clear();
  }, [setupScope]);

  useEffect(() => {
    if (remoteDrawer) {
      setRunners(EMPTY_RUNNERS);
      setSetup({});
      setRunnersLoading(false);
      return;
    }
    let cancelled = false;
    setRunnersLoading(true);
    void (async () => {
      const [configResult, setupResult] = await readHostState(runnersScopeId);
      if (cancelled) return;
      if (configResult.status === "fulfilled")
        setRunners(configResult.value.runners || EMPTY_RUNNERS);
      else message.error(t("acp.loadFailed"));
      if (setupResult.status === "fulfilled") {
        setSetup(setupResult.value.runners);
        setHost(setupResult.value.host);
        setJobs((previous) => ({
          ...previous,
          ...(setupResult.value.active_jobs ?? {}),
        }));
        setSetupFailed(false);
      } else {
        setSetup({});
        setSetupFailed(true);
      }
      setRunnersLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [t, message, runnersScopeId, remoteDrawer, setupReload]);

  const refreshHost = useCallback(async () => {
    if (!mounted.current || setupScopeRef.current !== setupScope) return null;
    const [configResult, setupResult] = await readHostState(runnersScopeId);
    if (!mounted.current || setupScopeRef.current !== setupScope) return null;
    if (configResult.status === "rejected") throw configResult.reason;
    if (setupResult.status === "rejected") throw setupResult.reason;
    const config = configResult.value;
    const view = setupResult.value;
    setRunners(config.runners || EMPTY_RUNNERS);
    setSetup(view.runners);
    setHost(view.host);
    setSetupFailed(false);
    setJobs((previous) => ({ ...previous, ...(view.active_jobs ?? {}) }));
    return view;
  }, [runnersScopeId, setupScope]);

  useEffect(() => {
    if (remoteDrawer || !pendingJobKey) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const results = await Promise.allSettled(
        pendingJobs.map((job) => acpApi.getJob(job.job_id)),
      );
      if (stopped) return;
      results.forEach((result, index) => {
        if (result.status === "fulfilled") {
          setJobs((previous) => ({
            ...previous,
            [result.value.runner]: result.value,
          }));
          setActionErrors((previous) =>
            previous[result.value.runner] === t("acp.jobPollFailed")
              ? { ...previous, [result.value.runner]: "" }
              : previous,
          );
        } else if (
          result.reason instanceof Error &&
          /^Request failed:\s*404(?:\s|$)/.test(result.reason.message)
        ) {
          const lost = pendingJobs[index];
          setJobs((previous) => ({
            ...previous,
            [lost.runner]: {
              ...(previous[lost.runner] ?? lost),
              status: "failed",
              phase: "failed",
              message: t("acp.jobInterrupted"),
              authorization: null,
              input_mode: "none",
            },
          }));
        } else {
          setActionErrors((previous) => ({
            ...previous,
            [pendingJobs[index].runner]: t("acp.jobPollFailed"),
          }));
        }
      });
      timer = setTimeout(() => void poll(), 1000);
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
    // The same job ids keep one sequential polling loop as output changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingJobKey, remoteDrawer, t]);

  useEffect(() => {
    if (remoteDrawer) return;
    for (const job of Object.values(jobs)) {
      if (isACPJobPending(job) || completedJobs.current.has(job.job_id))
        continue;
      completedJobs.current.add(job.job_id);
      void (async () => {
        try {
          const view = await refreshHost();
          if (!view) return;
          if (job.status === "succeeded") {
            message.success(
              t(
                job.kind === "connect"
                  ? "acp.connectSuccess"
                  : job.kind === "install"
                  ? "acp.installSuccess"
                  : "acp.loginSuccess",
              ),
            );
          } else if (job.status === "failed") {
            const text =
              job.message ||
              t(
                job.kind === "connect"
                  ? "acp.connectFailed"
                  : job.kind === "install"
                  ? "acp.installFailed"
                  : "acp.loginFailed",
              );
            setActionErrors((previous) => ({
              ...previous,
              [job.runner]: text,
            }));
            message.error(text);
          }
        } catch {
          if (mounted.current && setupScopeRef.current === setupScope) {
            setSetupFailed(true);
            setSetup((previous) => ({
              ...previous,
              [job.runner]: {
                ...previous[job.runner],
                connection: "unchecked",
                checked_at: null,
              },
            }));
            setActionErrors((previous) => ({
              ...previous,
              [job.runner]: t("acp.setupLoadFailed"),
            }));
          }
        }
      })();
    }
  }, [jobs, message, refreshHost, remoteDrawer, setupScope, t]);

  useEffect(() => {
    if (!activeAgentId) {
      setToolEnabled(false);
      setToolLoading(false);
      return;
    }

    const agentId = activeAgentId;
    setToolLoading(true);

    let cancelled = false;
    void (async () => {
      try {
        const data = await acpApi.getConfig(agentId);
        if (!cancelled && activeAgentIdRef.current === agentId) {
          setToolEnabled(data.tool_enabled);
        }
      } catch {
        if (!cancelled && activeAgentIdRef.current === agentId) {
          message.error(t("acp.loadFailed"));
        }
      } finally {
        if (!cancelled && activeAgentIdRef.current === agentId) {
          setToolLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [activeAgentId, t, message]);

  const orderedKeys = useMemo(() => {
    const keys = Object.keys(runners || {});
    return [
      ...BUILTIN_RUNNER_ORDER.filter((k) => keys.includes(k)),
      ...keys.filter((k) => !isBuiltinRunner(k)).sort(),
    ];
  }, [runners]);

  const cards = useMemo(() => {
    const enabled: { key: string; cfg: ACPRunnerConfig }[] = [];
    const disabled: { key: string; cfg: ACPRunnerConfig }[] = [];
    for (const key of orderedKeys) {
      const cfg = runners[key];
      if (!cfg) continue;
      (cfg.enabled ? enabled : disabled).push({ key, cfg });
    }
    return [...enabled, ...disabled];
  }, [runners, orderedKeys]);

  const persistRunners = useCallback(
    async (next: Record<string, ACPRunnerConfig>) => {
      const saved = await acpApi.updateGlobalRunners(next, runnersScopeId);
      setRunners(saved.runners || EMPTY_RUNNERS);
      return saved.runners;
    },
    [runnersScopeId],
  );

  const persistToolEnabled = useCallback(
    async (agentId: string, checked: boolean) => {
      const saved = await acpApi.updateToolEnabled(agentId, checked);
      if (activeAgentIdRef.current === agentId) {
        setToolEnabled(saved.tool_enabled);
      }
      return saved.tool_enabled;
    },
    [],
  );

  const handleToolToggle = async (checked: boolean) => {
    const agentId = activeAgentIdRef.current;
    if (!agentId || toolLoading) return;
    setToolToggleLoading(true);
    try {
      await persistToolEnabled(agentId, checked);
      message.success(t("acp.configSaved"));
    } catch {
      message.error(t("acp.configFailed"));
    } finally {
      setToolToggleLoading(false);
    }
  };

  const handleToggleEnabled = async (runnerKey: string, checked: boolean) => {
    // Runners are account-global; only block *enabling* under sandbox.
    if (
      remoteDrawer ||
      runnersLoading ||
      action ||
      toggleLoadingKey ||
      hostBusy ||
      (outboundBlocked && checked)
    )
      return;
    const runner = runners[runnerKey];
    if (!runner) return;
    if (checked && setup[runnerKey]?.connection !== "ready") {
      message.warning(t("acp.checkBeforeEnable"));
      return;
    }
    setToggleLoadingKey(runnerKey);
    try {
      const nextRunners = {
        ...runners,
        [runnerKey]: { ...runner, enabled: checked },
      };
      await persistRunners(nextRunners);
      message.success(t("acp.configSaved"));
    } catch {
      message.error(t("acp.configFailed"));
    } finally {
      setToggleLoadingKey(null);
    }
  };

  const checkConnection = async (runnerKey: string) => {
    const result = await acpApi.checkRunner(runnerKey);
    const view = await refreshHost();
    if (!view) return;
    setSetup((previous) => ({ ...previous, [runnerKey]: result }));
    if (result.connection === "ready") {
      message.success(t("acp.checkSuccess"));
      setGuideKey(null);
    } else if (result.connection === "auth_required") {
      if (result.message) message.warning(result.message);
      setGuideKey(runnerKey);
    } else {
      const text = result.message || t("acp.checkFailed");
      setActionErrors((previous) => ({ ...previous, [runnerKey]: text }));
      message.error(text);
    }
  };

  const handleSetupAction = async (
    runnerKey: string,
    type: "install" | "login" | "check" | "connect",
  ) => {
    if (type === "connect" && runnerKey !== "codex") return;
    if (
      remoteDrawer ||
      outboundBlocked ||
      action ||
      toggleLoadingKey ||
      hostBusy
    )
      return;
    setAction({ key: runnerKey, type });
    setActionErrors((previous) => ({ ...previous, [runnerKey]: "" }));
    try {
      if (type === "check") {
        await checkConnection(runnerKey);
      } else {
        const job = await (type === "connect"
          ? acpApi.connectCodex()
          : type === "install"
          ? acpApi.installRunner(runnerKey)
          : acpApi.loginRunner(runnerKey));
        if (!mounted.current || setupScopeRef.current !== setupScope) return;
        setJobs((previous) => ({ ...previous, [runnerKey]: job }));
        setSetup((previous) => ({
          ...previous,
          [runnerKey]: {
            ...previous[runnerKey],
            connection: "unchecked",
            checked_at: null,
          },
        }));
        setGuideKey(runnerKey);
      }
    } catch (error) {
      if (!mounted.current || setupScopeRef.current !== setupScope) return;
      const text =
        error instanceof Error
          ? error.message
          : t(
              type === "connect"
                ? "acp.connectFailed"
                : type === "install"
                ? "acp.installFailed"
                : type === "login"
                ? "acp.loginFailed"
                : "acp.checkFailed",
            );
      setActionErrors((previous) => ({ ...previous, [runnerKey]: text }));
      setSetup((previous) =>
        previous[runnerKey]
          ? {
              ...previous,
              [runnerKey]: {
                ...previous[runnerKey],
                connection: "unchecked",
                checked_at: null,
              },
            }
          : previous,
      );
      message.error(text);
    } finally {
      if (mounted.current) setAction(null);
    }
  };

  const handleCredentials = async (
    runnerKey: string,
    provider: string,
    apiKey: string,
  ) => {
    if (
      remoteDrawer ||
      outboundBlocked ||
      action ||
      toggleLoadingKey ||
      hostBusy
    )
      return;
    setAction({ key: runnerKey, type: "credentials" });
    setActionErrors((previous) => ({ ...previous, [runnerKey]: "" }));
    try {
      const result = await acpApi.saveCredentials(runnerKey, provider, apiKey);
      if (!mounted.current || setupScopeRef.current !== setupScope) return;
      setSetup((previous) => ({ ...previous, [runnerKey]: result }));
      await checkConnection(runnerKey);
    } catch (error) {
      if (!mounted.current || setupScopeRef.current !== setupScope) return;
      const text =
        error instanceof Error ? error.message : t("acp.credentialsFailed");
      setActionErrors((previous) => ({ ...previous, [runnerKey]: text }));
      message.error(text);
    } finally {
      if (mounted.current) setAction(null);
    }
  };

  const handleCancelJob = async (jobId: string) => {
    if (remoteDrawer || cancelLoading) return;
    setCancelLoading(true);
    try {
      const job = await acpApi.cancelJob(jobId);
      if (mounted.current && setupScopeRef.current === setupScope)
        setJobs((previous) => ({ ...previous, [job.runner]: job }));
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("acp.cancelJobFailed"),
      );
    } finally {
      if (mounted.current) setCancelLoading(false);
    }
  };

  const handleJobInput = async (jobId: string, data: string) => {
    const current = Object.values(jobs).find((job) => job.job_id === jobId);
    if (
      remoteDrawer ||
      inputLoading ||
      current?.kind !== "login" ||
      !isACPJobPending(current) ||
      current.input_mode !== "terminal"
    )
      return;
    setInputLoading(true);
    try {
      const job = await acpApi.inputJob(jobId, data);
      if (mounted.current && setupScopeRef.current === setupScope)
        setJobs((previous) => ({ ...previous, [job.runner]: job }));
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("acp.loginInputFailed"),
      );
    } finally {
      if (mounted.current) setInputLoading(false);
    }
  };

  const openEdit = (key: string) => {
    if (outboundBlocked) return;
    const cfg = runners[key];
    setIsCreateMode(false);
    setActiveKey(key);
    setDrawerOpen(true);
    form.setFieldsValue(runnerToFormValues(key, cfg));
  };

  const openCreate = () => {
    setIsCreateMode(true);
    setActiveKey(null);
    setDrawerOpen(true);
    form.resetFields();
    form.setFieldsValue({
      runnerKey: "",
      enabled: false,
      command: "",
      argsText: "",
      envText: "",
      trusted: true,
      tool_parse_mode: "update_detail",
      stdio_buffer_limit_bytes: ACP_DEFAULT_STDIO_BUFFER_LIMIT_BYTES,
    });
  };

  const handleSubmit = async (values: Record<string, unknown>) => {
    const targetKey = String(values.runnerKey || activeKey || "").trim();
    if (!targetKey) return;
    if ((isCreateMode || targetKey !== activeKey) && runners[targetKey]) {
      message.error(t("acp.runnerKeyExists"));
      return;
    }
    const runner = formValuesToRunner(values);
    const previous = activeKey ? runners[activeKey] : undefined;
    const launchChanged =
      !previous ||
      targetKey !== activeKey ||
      runner.command !== previous.command ||
      JSON.stringify(runner.args) !== JSON.stringify(previous.args) ||
      JSON.stringify(runner.env) !== JSON.stringify(previous.env);
    runner.enabled = launchChanged ? false : Boolean(previous?.enabled);
    setSaving(true);
    try {
      const nextRunners = { ...runners };
      if (!isCreateMode && activeKey && activeKey !== targetKey) {
        delete nextRunners[activeKey];
      }
      nextRunners[targetKey] = runner;
      await persistRunners(nextRunners);
      if (launchChanged) {
        setSetup((previousSetup) => {
          const next = { ...previousSetup };
          if (activeKey) delete next[activeKey];
          delete next[targetKey];
          return next;
        });
      }
      setSetupReload((previousReload) => previousReload + 1);
      setDrawerOpen(false);
      message.success(
        isCreateMode ? t("acp.createSuccess") : t("acp.configSaved"),
      );
    } catch {
      message.error(t("acp.configFailed"));
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = () => {
    if (!activeKey || isBuiltinRunner(activeKey)) return;
    modal.confirm({
      title: t("acp.deleteTitle", { name: activeKey }),
      content: t("acp.deleteConfirm"),
      okText: t("common.delete"),
      cancelText: t("common.cancel"),
      okButtonProps: { danger: true },
      onOk: async () => {
        const nextRunners = { ...runners };
        delete nextRunners[activeKey];
        await persistRunners(nextRunners);
        setDrawerOpen(false);
        message.success(t("acp.deleteSuccess"));
      },
    });
  };

  const toolToggleDisabled = !activeAgentId || toolLoading;

  const toolSwitch = (
    <Switch
      key={activeAgentId ?? "none"}
      checked={toolEnabled}
      loading={toolLoading || toolToggleLoading}
      disabled={toolToggleDisabled}
      onChange={handleToolToggle}
    />
  );

  return (
    <>
      {remoteDrawer ? (
        <Alert
          type="info"
          showIcon
          message={t("acp.remoteHostTitle")}
          description={t("acp.remoteHostHint")}
        />
      ) : (
        <>
          <div className={styles.toolbar}>
            <div className={styles.toolbarText}>
              <div className={styles.description}>{t("acp.description")}</div>
              <p className={styles.scopeHint}>{t("acp.globalRunnersHint")}</p>
            </div>
            <Button
              type="primary"
              disabled={Boolean(
                action || toggleLoadingKey || saving || hostBusy,
              )}
              onClick={openCreate}
            >
              {t("acp.create")}
            </Button>
          </div>

          <Alert
            className={styles.hostAlert}
            type="info"
            showIcon
            message={t("acp.executionHost", {
              host: host || t("acp.currentHost"),
            })}
            description={t("acp.hostHint")}
          />
          <p className={styles.scopeHint}>{t("acp.flowHint")}</p>
          {setupFailed ? (
            <Alert
              className={styles.hostAlert}
              type="warning"
              showIcon
              message={t("acp.setupLoadFailed")}
              action={
                <Button
                  size="small"
                  onClick={() => setSetupReload((value) => value + 1)}
                >
                  {t("acp.reloadSetup")}
                </Button>
              }
            />
          ) : null}

          {activeAgentId && outboundBlocked ? (
            <Alert
              type="info"
              showIcon
              className={styles.outboundBlockedAlert}
              message={t("acp.outboundBlockedHint")}
            />
          ) : null}

          {runnersLoading && cards.length === 0 ? (
            <CardSkeleton count={4} />
          ) : (
            <div className={channelStyles.channelsGrid}>
              {cards.map(({ key, cfg }) => (
                <ACPCard
                  key={key}
                  runnerKey={key}
                  config={cfg}
                  setup={setup[key]}
                  job={jobs[key]}
                  timeZone={timeZone}
                  isHover={hoverKey === key}
                  toggleLoading={toggleLoadingKey === key}
                  actionLoading={action?.key === key ? action.type : undefined}
                  busyElsewhere={Boolean(
                    (action && action.key !== key) ||
                      (toggleLoadingKey && toggleLoadingKey !== key) ||
                      saving ||
                      runnersLoading ||
                      (hostBusy && !isACPJobPending(jobs[key])),
                  )}
                  interactionDisabled={outboundBlocked}
                  onClick={() => openEdit(key)}
                  onMouseEnter={() => setHoverKey(key)}
                  onMouseLeave={() => setHoverKey(null)}
                  onToggleEnabled={handleToggleEnabled}
                  onInstall={(runnerKey) =>
                    void handleSetupAction(runnerKey, "install")
                  }
                  onConnect={(runnerKey) =>
                    void handleSetupAction(runnerKey, "connect")
                  }
                  onCheck={(runnerKey) =>
                    void handleSetupAction(runnerKey, "check")
                  }
                  onGuide={setGuideKey}
                />
              ))}
            </div>
          )}
          {Object.entries(actionErrors)
            .filter(([, text]) => text)
            .map(([key, text]) => (
              <Alert
                className={styles.hostAlert}
                key={key}
                type="error"
                showIcon
                message={key}
                description={text}
              />
            ))}
        </>
      )}

      {expertScoped ? (
        <div className={styles.agentToolSection}>
          <p className={styles.scopeHint}>{t("acp.toolEnabledHint")}</p>
          {!activeAgentId ? (
            <Empty
              description={t("mbtiPage.pickAgent")}
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ) : (
            <div className={styles.toolToggle}>
              <span>{t("acp.toolEnabled")}</span>
              {toolSwitch}
            </div>
          )}
        </div>
      ) : null}

      <ACPDrawer
        open={drawerOpen}
        activeKey={activeKey}
        isCreateMode={isCreateMode}
        form={form}
        saving={saving}
        canEditKey={
          isCreateMode || (activeKey ? !isBuiltinRunner(activeKey) : false)
        }
        canDelete={
          !isCreateMode && activeKey !== null && !isBuiltinRunner(activeKey)
        }
        onClose={() => setDrawerOpen(false)}
        onSubmit={handleSubmit}
        onDelete={handleDelete}
      />
      <ACPSetupGuide
        runnerKey={guideKey}
        setup={guideKey ? setup[guideKey] : undefined}
        timeZone={timeZone}
        host={host}
        job={guideKey ? jobs[guideKey] : undefined}
        operation={action?.key === guideKey ? action.type : undefined}
        busy={Boolean(
          outboundBlocked ||
            (hostBusy && (!guideKey || !isACPJobPending(jobs[guideKey]))),
        )}
        inputLoading={inputLoading}
        cancelLoading={cancelLoading}
        error={guideKey ? actionErrors[guideKey] : undefined}
        onClose={() => setGuideKey(null)}
        onCheck={(runnerKey) => void handleSetupAction(runnerKey, "check")}
        onInstall={(runnerKey) => void handleSetupAction(runnerKey, "install")}
        onLogin={(runnerKey) => void handleSetupAction(runnerKey, "login")}
        onConnect={(runnerKey) => void handleSetupAction(runnerKey, "connect")}
        onCredentials={handleCredentials}
        onCancel={(jobId) => void handleCancelJob(jobId)}
        onInput={handleJobInput}
      />
    </>
  );
}

/** Standalone ACP page (sidebar Control entry). */
export default function ACPPage() {
  const { t } = useTranslation();
  return (
    <PageShell
      title={t("pageShell.acp.title")}
      subtitle={t("pageShell.acp.subtitle")}
    >
      <ACPPanel />
    </PageShell>
  );
}
