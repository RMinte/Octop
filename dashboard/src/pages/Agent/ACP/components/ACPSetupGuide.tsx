import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Form,
  Input,
  Select,
  Space,
  Spin,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  isACPJobPending,
  type ACPRunnerSetup,
  type ACPSetupJob,
} from "../../../../api/types/acp";
import { runnerLabelKey } from "../constants";
import { formatServerDateTime } from "../../../../utils/formatMessageTime";
import { stripAnsi } from "../../../Control/Terminal/terminalOutputBuffer";
import styles from "../index.module.less";

interface ACPSetupGuideProps {
  runnerKey: string | null;
  setup?: ACPRunnerSetup;
  job?: ACPSetupJob;
  host: string;
  timeZone: string;
  operation?: "install" | "login" | "check" | "credentials" | "connect";
  busy?: boolean;
  inputLoading?: boolean;
  cancelLoading?: boolean;
  error?: string;
  onClose: () => void;
  onInstall: (runnerKey: string) => void;
  onLogin: (runnerKey: string) => void;
  onConnect: (runnerKey: string) => void;
  onCheck: (runnerKey: string) => void;
  onCancel: (jobId: string) => void;
  onInput: (jobId: string, data: string) => Promise<void>;
  onCredentials: (
    runnerKey: string,
    provider: string,
    apiKey: string,
  ) => Promise<void>;
}

function CredentialsForm({
  options,
  disabled,
  saving,
  onSave,
}: {
  options: Array<{ id: string; label: string }>;
  disabled: boolean;
  saving: boolean;
  onSave: (provider: string, apiKey: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const saveCredentials = async (values: {
    provider: string;
    apiKey: string;
  }) => {
    try {
      await onSave(values.provider, values.apiKey);
    } finally {
      form.setFieldValue("apiKey", "");
    }
  };
  return (
    <Form
      form={form}
      layout="vertical"
      initialValues={{ provider: options[0]?.id }}
      onFinish={(values) => void saveCredentials(values)}
    >
      <Form.Item
        name="provider"
        label={t("acp.credentialProvider")}
        rules={[
          {
            required: true,
            message: t("acp.credentialProviderRequired"),
          },
        ]}
      >
        <Select
          disabled={disabled}
          options={options.map((option) => ({
            value: option.id,
            label: option.label,
          }))}
        />
      </Form.Item>
      <Form.Item
        name="apiKey"
        label="API Key"
        rules={[
          {
            required: true,
            whitespace: true,
            message: t("acp.apiKeyRequired"),
          },
        ]}
      >
        <Input.Password autoComplete="new-password" disabled={disabled} />
      </Form.Item>
      <Button
        type="primary"
        htmlType="submit"
        disabled={disabled}
        loading={saving}
      >
        {t("acp.saveCredentialsAndCheck")}
      </Button>
    </Form>
  );
}

/** Setup runs on the box; the browser only authorizes or answers its login process. */
export function ACPSetupGuide({
  runnerKey,
  setup,
  job,
  host,
  timeZone,
  operation,
  busy,
  inputLoading,
  cancelLoading,
  error,
  onClose,
  onInstall,
  onLogin,
  onConnect,
  onCheck,
  onCancel,
  onInput,
  onCredentials,
}: ACPSetupGuideProps) {
  const { t } = useTranslation();
  const [response, setResponse] = useState("");
  const output = useRef<HTMLPreElement | null>(null);
  const labelKey = runnerKey ? runnerLabelKey(runnerKey) : undefined;
  const pending = isACPJobPending(job);
  const codex = runnerKey === "codex";
  const installed = setup?.installation === "installed";
  const connected = setup?.enabled && setup.connection === "ready";
  const locked = Boolean(busy || operation || pending);
  const options = setup?.credential_options ?? [];
  const jobMessage =
    codex &&
    pending &&
    setup?.connection === "auth_required" &&
    job?.message &&
    job.message === setup.message
      ? t("acp.codexAuthorizationHint")
      : job?.message;

  useEffect(() => {
    setResponse("");
  }, [runnerKey]);

  useEffect(() => {
    if (output.current) output.current.scrollTop = output.current.scrollHeight;
  }, [job?.output]);

  const sendResponse = async (data: string) => {
    if (
      !job ||
      !pending ||
      job.kind !== "login" ||
      job.input_mode !== "terminal"
    )
      return;
    setResponse("");
    await onInput(job.job_id, data);
  };

  return (
    <Drawer
      title={t("acp.setupGuideTitle", {
        name: labelKey ? t(labelKey) : runnerKey || "",
      })}
      open={Boolean(runnerKey)}
      onClose={onClose}
      width={620}
      destroyOnHidden
      footer={
        codex ? (
          !pending &&
          (!connected ||
            job?.status === "failed" ||
            job?.status === "cancelled") ? (
            <Button
              type="primary"
              loading={operation === "connect"}
              disabled={!setup || locked}
              onClick={() => runnerKey && onConnect(runnerKey)}
            >
              {t(
                job?.status === "failed" || job?.status === "cancelled"
                  ? "acp.retryConnectCodex"
                  : "acp.connectCodex",
              )}
            </Button>
          ) : null
        ) : (
          <Button
            type="primary"
            loading={operation === "check"}
            disabled={!setup || !installed || locked}
            onClick={() => runnerKey && onCheck(runnerKey)}
          >
            {t("acp.checkConnection")}
          </Button>
        )
      }
    >
      <Alert
        type="info"
        showIcon
        message={t("acp.executionHost", { host: host || t("acp.currentHost") })}
        description={t("acp.hostHint")}
      />
      <p className={styles.guideStep}>
        {t(codex ? "acp.codexConnectHint" : "acp.boxSetupHint")}
      </p>
      {codex ? (
        <p className={styles.scopeHint}>{t("acp.codexDeviceCodeHint")}</p>
      ) : null}
      {error && error !== job?.message ? (
        <Alert
          className={styles.hostAlert}
          type="error"
          showIcon
          message={error}
        />
      ) : null}
      {job ? (
        <div className={styles.jobPanel}>
          <div className={styles.jobHeading} role="status">
            {pending ? <Spin size="small" /> : null}
            <strong>{t(`acp.job_${job.kind}_${job.status}`)}</strong>
          </div>
          {job.phase ? (
            <p className={styles.setupMessage}>{t(`acp.phase_${job.phase}`)}</p>
          ) : null}
          {jobMessage ? (
            <Alert
              className={styles.hostAlert}
              type={
                job.status === "failed"
                  ? "error"
                  : job.status === "succeeded"
                  ? "success"
                  : "info"
              }
              showIcon
              message={jobMessage}
            />
          ) : null}
          {pending && job.authorization ? (
            <div className={styles.authorizationBox}>
              <p>{t("acp.authorizeInBrowser")}</p>
              {job.authorization.user_code ? (
                <Typography.Paragraph copyable className={styles.deviceCode}>
                  {job.authorization.user_code}
                </Typography.Paragraph>
              ) : null}
              {job.authorization.url ? (
                <Button
                  type="primary"
                  href={job.authorization.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t("acp.openAuthorization")}
                </Button>
              ) : null}
              {job.authorization.expires_at ? (
                <p className={styles.checkedAt}>
                  {t("acp.authorizationExpires", {
                    time: formatServerDateTime(
                      job.authorization.expires_at / 1000,
                      timeZone,
                    ),
                  })}
                </p>
              ) : null}
              <p className={styles.scopeHint}>
                {t("acp.authorizationWaiting")}
              </p>
            </div>
          ) : null}
          {job.output ? (
            <pre
              ref={output}
              role="log"
              aria-label={t("acp.jobOutput")}
              className={styles.jobOutput}
            >
              {stripAnsi(job.output)}
            </pre>
          ) : null}
          {!codex &&
          pending &&
          job.kind === "login" &&
          job.input_mode === "terminal" ? (
            <div className={styles.loginResponse}>
              <label htmlFor="acp-login-response">
                {t("acp.loginResponse")}
              </label>
              <Input.Password
                id="acp-login-response"
                autoComplete="off"
                value={response}
                disabled={inputLoading}
                onChange={(event) => setResponse(event.target.value)}
                onPressEnter={() => void sendResponse(`${response}\r`)}
              />
              <Space wrap>
                <Button
                  loading={inputLoading}
                  onClick={() => void sendResponse(`${response}\r`)}
                >
                  {t("acp.submitLoginResponse")}
                </Button>
                <Button
                  disabled={inputLoading}
                  onClick={() => void sendResponse("\u001b[A")}
                >
                  {t("acp.previousLoginOption")}
                </Button>
                <Button
                  disabled={inputLoading}
                  onClick={() => void sendResponse("\u001b[B")}
                >
                  {t("acp.nextLoginOption")}
                </Button>
              </Space>
              <p className={styles.scopeHint}>{t("acp.loginResponseHint")}</p>
            </div>
          ) : null}
          {pending ? (
            <Button
              danger
              disabled={job.phase === "enabling"}
              loading={cancelLoading}
              onClick={() => onCancel(job.job_id)}
            >
              {t("acp.cancelJob")}
            </Button>
          ) : null}
          {!codex && !pending && job.status === "failed" ? (
            <Button
              disabled={locked}
              onClick={() =>
                runnerKey &&
                (job.kind === "install"
                  ? onInstall(runnerKey)
                  : onLogin(runnerKey))
              }
            >
              {t("acp.retryJob")}
            </Button>
          ) : null}
          {pending ? (
            <p className={styles.checkedAt}>{t("acp.jobBackgroundHint")}</p>
          ) : null}
        </div>
      ) : null}
      {!codex && !installed && setup?.can_install ? (
        <Button
          type="primary"
          loading={operation === "install"}
          disabled={locked}
          onClick={() => runnerKey && onInstall(runnerKey)}
        >
          {t("acp.installOnBox")}
        </Button>
      ) : null}
      {!codex && installed ? (
        <>
          {setup?.can_login ? (
            <div className={styles.guideStep}>
              <Button
                type="primary"
                loading={operation === "login"}
                disabled={locked}
                onClick={() => runnerKey && onLogin(runnerKey)}
              >
                {t("acp.startLogin")}
              </Button>
              <p className={styles.scopeHint}>{t("acp.browserLoginHint")}</p>
            </div>
          ) : null}
          {options.length ? (
            <div className={styles.guideStep}>
              <Typography.Title level={5}>
                {t("acp.apiKeyTitle")}
              </Typography.Title>
              <CredentialsForm
                key={runnerKey}
                options={options}
                disabled={locked}
                saving={operation === "credentials"}
                onSave={(provider, apiKey) =>
                  runnerKey
                    ? onCredentials(runnerKey, provider, apiKey)
                    : Promise.resolve()
                }
              />
              <p className={styles.scopeHint}>{t("acp.credentialsHint")}</p>
            </div>
          ) : null}
          {!setup?.can_login && !options.length ? (
            <p>{t("acp.customSetupHint")}</p>
          ) : null}
        </>
      ) : null}
      {!codex && !installed && !setup?.can_install ? (
        <p>{t("acp.customSetupHint")}</p>
      ) : null}
      {setup?.docs_url ? (
        <Typography.Link
          href={setup.docs_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t("acp.openOfficialDocs")}
        </Typography.Link>
      ) : null}
      <p className={styles.guideStep}>{t("acp.checkScopeHint")}</p>
      {setup?.checked_at ? (
        <p className={styles.scopeHint}>
          {t("acp.lastChecked", {
            time: formatServerDateTime(setup.checked_at / 1000, timeZone),
          })}
        </p>
      ) : null}
    </Drawer>
  );
}
