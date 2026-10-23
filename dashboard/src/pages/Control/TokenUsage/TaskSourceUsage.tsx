import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Spin,
  Table,
  Tag,
} from "antd";
import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  taskUsageApi,
  type ReferenceCostAmounts,
  type RetainedSourceTotals,
  type TaskCallUsage,
  type TaskUsageItem,
  type TaskUsagePage,
  type TaskUsageUserFilter,
} from "../../../api/modules/taskUsage";

export default function TaskSourceUsage({
  windowKey,
  adminUserFilter,
}: {
  windowKey: string;
  adminUserFilter: TaskUsageUserFilter;
}) {
  const { t } = useTranslation();
  const scopeKey = JSON.stringify([windowKey, adminUserFilter]);
  const [pagination, setPagination] = useState({
    scopeKey,
    offset: 0,
    limit: 20,
  });
  const { offset, limit } =
    pagination.scopeKey === scopeKey ? pagination : { offset: 0, limit: 20 };
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<TaskUsagePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setData(null);
    setError(null);
    void taskUsageApi
      .summary({
        window: windowKey,
        userFilter: adminUserFilter,
        offset,
        limit,
      })
      .then((page) => {
        if (!cancelled) setData(page);
      })
      .catch((reason: unknown) => {
        if (!cancelled)
          setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [windowKey, adminUserFilter, offset, limit, revision]);

  const count = (value: number | null, noData = false) =>
    value === null
      ? t(noData ? "tokenUsage.sourceNoRecords" : "tokenUsage.sourceUnknown")
      : value.toLocaleString();
  const callTokens = (usage: TaskCallUsage) => (
    <span>
      {count(usage.tokens.total_tokens, usage.calls === 0)}
      {usage.coverage.total_tokens === "partial" && (
        <small> · {t("tokenUsage.sourceKnownSubtotal")}</small>
      )}
    </span>
  );
  const retainedTokens = (usage: RetainedSourceTotals) =>
    count(usage.total_tokens, usage.usage_records === 0);
  const cost = (value: ReferenceCostAmounts) => {
    if (value.status === "no_data") return t("tokenUsage.sourceNoRecords");
    if (value.status === "estimated" && value.amount_usd !== null)
      return `$${value.amount_usd}`;
    if (
      value.status === "estimated_range" &&
      value.lower_bound_usd !== null &&
      value.upper_bound_usd !== null
    )
      return (
        <span>
          ${value.lower_bound_usd}–${value.upper_bound_usd}
          <small> · {t("tokenUsage.sourceCostRange")}</small>
        </span>
      );
    const subtotal = value.known_amount_usd ?? value.lower_bound_usd;
    if (
      (value.status === "partial" || value.status === "unknown") &&
      subtotal !== null
    )
      return (
        <span>
          ${subtotal}
          <small> · {t("tokenUsage.sourcePartialCost")}</small>
        </span>
      );
    return t("tokenUsage.sourceUnknown");
  };
  const retained = data?.totals?.retained_task_usage;
  const calls = data?.totals?.call_usage;

  return (
    <Card
      size="small"
      title={t("tokenUsage.sourceTitle")}
      style={{ marginTop: 16, flexShrink: 0 }}
      extra={
        <Button
          icon={<RefreshCw size={14} />}
          onClick={() => setRevision((value) => value + 1)}
          loading={loading}
        >
          {t("tokenUsage.sourceRefresh")}
        </Button>
      }
    >
      <Alert
        type="info"
        showIcon
        message={t("tokenUsage.sourceDescription")}
        style={{ marginBottom: 12 }}
      />
      {error && <Alert type="error" showIcon message={error} />}
      {loading ? (
        <div style={{ padding: 24, textAlign: "center" }}>
          <Spin />
        </div>
      ) : data ? (
        <>
          {retained && calls && (
            <div role="group" aria-label={t("tokenUsage.sourceAllTotals")}>
              <Descriptions
                size="small"
                title={t("tokenUsage.sourceAllTotals")}
                column={{ xs: 1, sm: 2, lg: 3 }}
              >
                <Descriptions.Item label={t("tokenUsage.sourceTaskCount")}>
                  {data.total_tasks.toLocaleString()}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceRetainedTotal")}>
                  {count(
                    retained.total_tokens,
                    retained.coverage === "no_data",
                  )}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceRecordedTotal")}>
                  {callTokens(calls)}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceRecordedBox")}>
                  {callTokens(data.totals.sources.box)}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceRecordedThird")}>
                  {callTokens(data.totals.sources.third_party)}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceMissingCalls")}>
                  {calls.calls
                    ? calls.missing_calls.total_tokens.toLocaleString()
                    : t("tokenUsage.sourceNoRecords")}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceInferredBox")}>
                  {retainedTokens(retained.sources.box)}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceInferredThird")}>
                  {retainedTokens(retained.sources.third_party)}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceUnattributed")}>
                  {retainedTokens(retained.unattributed_usage)}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceRecordedCost")}>
                  {cost(calls.reference_cost)}
                </Descriptions.Item>
                <Descriptions.Item label={t("tokenUsage.sourceRetainedCost")}>
                  {cost(retained.reference_cost)}
                </Descriptions.Item>
              </Descriptions>
            </div>
          )}
          {data.unassigned_call_usage.calls > 0 && (
            <Alert
              type="warning"
              message={t("tokenUsage.sourceUnassignedCalls", {
                count: data.unassigned_call_usage.calls,
              })}
              style={{ marginBottom: 12 }}
            />
          )}
          {data.items.length === 0 ? (
            <Empty description={t("tokenUsage.noRecordsInRange")} />
          ) : (
            <Table<TaskUsageItem>
              rowKey="task_id"
              size="small"
              dataSource={data.items}
              scroll={{ x: "max-content" }}
              pagination={{
                current: Math.floor(offset / limit) + 1,
                pageSize: limit,
                total: data.total_tasks,
                showSizeChanger: true,
                pageSizeOptions: [20, 50, 100],
                onChange: (page, pageSize) =>
                  setPagination({
                    scopeKey,
                    offset: pageSize === limit ? (page - 1) * pageSize : 0,
                    limit: pageSize,
                  }),
              }}
              columns={[
                {
                  title: t("tokenUsage.task"),
                  key: "task",
                  render: (_, item) => (
                    <div style={{ maxWidth: 280, overflowWrap: "anywhere" }}>
                      <div>{item.title || item.task_id}</div>
                      <small>{item.task_id}</small>
                      {item.project_name && <div>{item.project_name}</div>}
                    </div>
                  ),
                },
                {
                  title: t("tokenUsage.sourceRetainedTotal"),
                  key: "retained",
                  render: (_, item) =>
                    count(
                      item.retained_task_usage.total_tokens,
                      item.retained_task_usage.coverage === "no_data",
                    ),
                },
                ...(["box", "third_party"] as const).map((source) => ({
                  title: t(
                    source === "box"
                      ? "tokenUsage.sourceBox"
                      : "tokenUsage.sourceThird",
                  ),
                  key: source,
                  render: (_: unknown, item: TaskUsageItem) =>
                    item.call_usage.calls > 0
                      ? callTokens(item.sources[source])
                      : retainedTokens(
                          item.retained_task_usage.sources[source],
                        ),
                })),
                {
                  title: t("tokenUsage.sourceMissingCalls"),
                  key: "missing",
                  render: (_, item) =>
                    item.call_usage.calls > 0
                      ? item.call_usage.missing_calls.total_tokens.toLocaleString()
                      : t("tokenUsage.sourceNoRecords"),
                },
                {
                  title: t("tokenUsage.sourceReferenceCost"),
                  key: "cost",
                  render: (_, item) =>
                    cost(
                      item.call_usage.calls > 0
                        ? item.call_usage.reference_cost
                        : item.retained_task_usage.reference_cost,
                    ),
                },
                {
                  title: t("tokenUsage.sourceBasis"),
                  key: "basis",
                  render: (_, item) => (
                    <div>
                      <Tag>
                        {t(
                          item.call_usage.calls > 0
                            ? "tokenUsage.sourceRecorded"
                            : item.retained_task_usage.source_attribution ===
                              "no_data"
                            ? "tokenUsage.sourceNoRecords"
                            : "tokenUsage.sourceInferred",
                        )}
                      </Tag>
                      {item.call_usage.calls === 0 &&
                        item.retained_task_usage.unattributed_usage
                          .usage_records > 0 && (
                          <div>
                            {t("tokenUsage.sourceUnattributed")}:{" "}
                            {retainedTokens(
                              item.retained_task_usage.unattributed_usage,
                            )}
                          </div>
                        )}
                    </div>
                  ),
                },
              ]}
            />
          )}
        </>
      ) : null}
    </Card>
  );
}
