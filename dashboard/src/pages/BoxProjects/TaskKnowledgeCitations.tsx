import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Alert, Button, Drawer, Space, Spin, Typography } from "antd";
import { FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  knowledgeBasesApi,
  type KnowledgeDocument,
} from "../../api/modules/knowledgeBases";
import LazyMarkdown from "../../components/Markdown/LazyMarkdown";
import { getDocKind } from "../../utils/docKind";
import { knowledgeCitationTooltip } from "../../utils/knowledgeCitationDisplay";
import {
  canDownloadKnowledgeOriginal,
  canRichPreviewKnowledgeDocument,
  isEditableKnowledgeDocument,
  isKnowledgeMarkdownDocument,
} from "../../utils/knowledgeDocPreview";
import { stripFrontmatter } from "../../utils/markdown";
import type { KnowledgeCitation } from "../../utils/parseKnowledgeCitations";
import styles from "./index.module.less";

const DocumentPreviewCore = lazy(
  () => import("../../components/DocumentPreviewCore"),
);
const validCitation = (citation: KnowledgeCitation) =>
  /^[A-Za-z0-9_-]+$/.test(citation.kbId) &&
  /^[A-Za-z0-9_-]+$/.test(citation.docId);

function TaskKnowledgePreview({ citation }: { citation: KnowledgeCitation }) {
  const { t } = useTranslation();
  const [document, setDocument] = useState<KnowledgeDocument | null>(null);
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [downloadError, setDownloadError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const downloadRequest = useRef<AbortController | null>(null);
  const kind = document ? getDocKind(document.filename) : null;
  const rich =
    document &&
    kind &&
    kind !== "ppt" &&
    canRichPreviewKnowledgeDocument(document);

  useEffect(
    () => () => {
      downloadRequest.current?.abort();
      downloadRequest.current = null;
    },
    [citation.kbId, citation.docId],
  );

  useEffect(() => {
    let disposed = false;
    setLoading(true);
    setError("");
    void (async () => {
      const documents = await knowledgeBasesApi.listDocuments(citation.kbId);
      const resolved = documents.find(
        (item) => item.id === citation.docId && !item.is_dir,
      );
      if (!resolved) throw new Error("unavailable citation");
      if (disposed) return;
      setDocument(resolved);
      const docKind = getDocKind(resolved.filename);
      if (
        docKind &&
        docKind !== "ppt" &&
        canRichPreviewKnowledgeDocument(resolved)
      )
        return;
      const preview =
        isEditableKnowledgeDocument(resolved) ||
        isKnowledgeMarkdownDocument(resolved)
          ? await knowledgeBasesApi.getTextDocument(
              citation.kbId,
              citation.docId,
            )
          : await knowledgeBasesApi.previewDocument(
              citation.kbId,
              citation.docId,
            );
      if (!disposed) setText(preview.text);
    })()
      .catch(() => {
        if (!disposed) setError(t("projects.tasks.knowledge.previewFailed"));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [citation.kbId, citation.docId, t]);

  const fetchBlob = useCallback(
    async (
      onProgress?: (loaded: number, total: number) => void,
      signal?: AbortSignal,
    ) => {
      try {
        return await knowledgeBasesApi.fetchDocumentFile(
          citation.kbId,
          citation.docId,
          "inline",
          onProgress,
          signal,
        );
      } catch (err) {
        if (!signal?.aborted)
          setError(t("projects.tasks.knowledge.previewFailed"));
        throw err;
      }
    },
    [citation.kbId, citation.docId, t],
  );

  const download = async () => {
    downloadRequest.current?.abort();
    const controller = new AbortController();
    downloadRequest.current = controller;
    setDownloading(true);
    setDownloadError("");
    try {
      const blob = await knowledgeBasesApi.fetchDocumentFile(
        citation.kbId,
        citation.docId,
        "attachment",
        undefined,
        controller.signal,
      );
      if (controller.signal.aborted || downloadRequest.current !== controller)
        return;
      const url = URL.createObjectURL(blob);
      const link = window.document.createElement("a");
      link.href = url;
      link.download = document?.filename || citation.filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      if (
        !controller.signal.aborted &&
        downloadRequest.current === controller &&
        !(error instanceof Error && error.name === "AbortError")
      ) {
        setDownloadError(t("projects.tasks.knowledge.downloadFailed"));
      }
    } finally {
      if (
        !controller.signal.aborted &&
        downloadRequest.current === controller
      ) {
        downloadRequest.current = null;
        setDownloading(false);
      }
    }
  };

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!document) return null;
  return (
    <>
      <Space direction="vertical" style={{ width: "100%", marginBottom: 12 }}>
        <Typography.Text type="secondary">
          {citation.kbName} · {document.path || document.filename}
        </Typography.Text>
        {canDownloadKnowledgeOriginal(document) && (
          <Button loading={downloading} onClick={() => void download()}>
            {t("projects.tasks.knowledge.download")}
          </Button>
        )}
        {downloadError && <Alert type="error" message={downloadError} />}
      </Space>
      {rich && kind ? (
        <div className={styles.taskKnowledgePreviewRich}>
          <Suspense fallback={<Spin />}>
            <DocumentPreviewCore
              kind={kind}
              filename={document.filename}
              fetchBlob={fetchBlob}
              onDownload={
                canDownloadKnowledgeOriginal(document) ? download : undefined
              }
            />
          </Suspense>
        </div>
      ) : isKnowledgeMarkdownDocument(document) ? (
        <LazyMarkdown content={stripFrontmatter(text)} />
      ) : (
        <pre className={styles.taskKnowledgePreviewText}>
          {text || t("projects.tasks.knowledge.emptyPreview")}
        </pre>
      )}
    </>
  );
}

export default function TaskKnowledgeCitations({
  citations,
}: {
  citations: KnowledgeCitation[];
}) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<KnowledgeCitation | null>(null);
  if (!citations.length) return null;
  return (
    <section
      className={styles.taskKnowledgeSources}
      aria-label={t("projects.tasks.knowledge.sources")}
    >
      <Typography.Text type="secondary">
        {t("projects.tasks.knowledge.sources")}
      </Typography.Text>
      <div className={styles.taskKnowledgeItems}>
        {citations.map((citation) => (
          <Button
            key={`${citation.kbId}:${citation.docId}`}
            size="small"
            icon={<FileText size={14} aria-hidden="true" />}
            title={
              validCitation(citation)
                ? knowledgeCitationTooltip(citation)
                : t("projects.tasks.knowledge.invalidSource")
            }
            disabled={!validCitation(citation)}
            onClick={() => setSelected(citation)}
          >
            {citation.filename}
            {citation.kbName ? ` · ${citation.kbName}` : ""}
          </Button>
        ))}
      </div>
      <Drawer
        open={selected != null}
        onClose={() => setSelected(null)}
        title={selected?.filename}
        width={800}
        destroyOnHidden
      >
        {selected && (
          <TaskKnowledgePreview
            key={`${selected.kbId}:${selected.docId}`}
            citation={selected}
          />
        )}
      </Drawer>
    </section>
  );
}
