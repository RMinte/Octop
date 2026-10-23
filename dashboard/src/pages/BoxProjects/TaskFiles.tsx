import { useTaskHost } from "../../context/TaskHostContext";
import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Dropdown,
  Empty,
  Input,
  Modal,
  Space,
  Spin,
  Typography,
} from "antd";
import { Download, File, Folder, RefreshCw, Save, Upload } from "lucide-react";
import { useTranslation } from "react-i18next";
import { type TaskWorkbenchScope } from "../../api/modules/taskWorkbench";
import type { FileTreeNode } from "../../api/types";
import FileViewer from "../Agent/Workspace/components/FileViewer";
import { isProbablyText } from "../Agent/Workspace/utils/fileKind";
import { getDocKind } from "../Agent/Workspace/utils/docKind";
import { getMediaKind } from "../Agent/Workspace/utils/mediaKind";
import { apiErrorMessage } from "../../utils/apiError";

export default function TaskFiles({
  scope,
  onDirtyChange,
}: {
  scope: TaskWorkbenchScope;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const { taskWorkbenchApi } = useTaskHost();
  const { t } = useTranslation();
  const [directory, setDirectory] = useState(".");
  const [nodes, setNodes] = useState<FileTreeNode[]>([]);
  const [selected, setSelected] = useState<FileTreeNode | null>(null);
  const [text, setText] = useState("");
  const [saved, setSaved] = useState("");
  const [fileRevision, setFileRevision] = useState<string>();
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [fileLoading, setFileLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [operation, setOperation] = useState<
    "file" | "folder" | "move" | "delete" | null
  >(null);
  const [destination, setDestination] = useState("");
  const uploadRef = useRef<HTMLInputElement>(null);
  const dirty = text !== saved;
  const writable = !scope.read_only;
  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);
  const editable =
    !!selected &&
    fileRevision !== undefined &&
    !selected.is_dir &&
    isProbablyText(selected.path) &&
    !getMediaKind(selected.path) &&
    !getDocKind(selected.path);
  const fail = (err: unknown) =>
    setError(apiErrorMessage(err, t("projects.tasks.failure"), t));
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void taskWorkbenchApi
      .tree(scope.task_id, directory, scope.scope_revision, controller.signal)
      .then(setNodes)
      .catch((err: unknown) => {
        if (!controller.signal.aborted) fail(err);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope.task_id, scope.scope_revision, directory, version]);
  useEffect(() => {
    const controller = new AbortController();
    setText("");
    setSaved("");
    setFileRevision(undefined);
    setEditing(false);
    if (
      !selected ||
      selected.is_dir ||
      !isProbablyText(selected.path) ||
      getMediaKind(selected.path) ||
      getDocKind(selected.path)
    ) {
      setFileLoading(false);
      return;
    }
    setFileLoading(true);
    void taskWorkbenchApi
      .file(
        scope.task_id,
        selected.path,
        scope.scope_revision,
        controller.signal,
      )
      .then((file) => {
        setText(file.content);
        setSaved(file.content);
        setFileRevision(file.revision);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) fail(err);
      })
      .finally(() => {
        if (!controller.signal.aborted) setFileLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope.task_id, scope.scope_revision, selected]);
  const leave = (next: () => void) => {
    if (!dirty) {
      next();
      return;
    }
    Modal.confirm({
      title: t("projects.tasks.workbench.unsaved"),
      content: t("projects.tasks.workbench.discardHint"),
      okText: t("projects.tasks.workbench.discard"),
      cancelText: t("projects.cancel"),
      onOk: next,
    });
  };
  const run = async (action: () => Promise<unknown>, done?: () => void) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await action();
      done?.();
      setVersion((v) => v + 1);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };
  const mutate = async () => {
    const name = destination.trim();
    if (!operation || (operation !== "delete" && !name)) return;
    await run(
      async () => {
        if (operation === "file")
          await taskWorkbenchApi.save(
            scope.task_id,
            name,
            "",
            scope.scope_revision,
            "missing",
          );
        if (operation === "folder")
          await taskWorkbenchApi.mkdir(
            scope.task_id,
            name,
            scope.scope_revision,
          );
        if (operation === "move" && selected)
          await taskWorkbenchApi.move(
            scope.task_id,
            selected.path,
            name,
            scope.scope_revision,
          );
        if (operation === "delete" && selected)
          await taskWorkbenchApi.delete(
            scope.task_id,
            selected.path,
            scope.scope_revision,
          );
      },
      () => {
        setOperation(null);
        setSelected(null);
      },
    );
  };
  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          closable
          onClose={() => setError(null)}
        />
      )}
      <Space wrap>
        <Typography.Text code>{directory}</Typography.Text>
        <Button
          size="small"
          disabled={directory === "." || busy}
          onClick={() =>
            leave(() => {
              setSelected(null);
              const parent = directory.split("/").slice(0, -1).join("/");
              setDirectory(parent || ".");
            })
          }
        >
          {t("projects.tasks.workbench.parent")}
        </Button>
        <Button
          size="small"
          icon={<RefreshCw size={14} />}
          loading={loading}
          onClick={() => setVersion((v) => v + 1)}
        >
          {t("projects.tasks.workbench.refresh")}
        </Button>
        {writable && (
          <>
            <Button
              size="small"
              onClick={() => {
                setDestination(directory === "." ? "" : `${directory}/`);
                setOperation("file");
              }}
            >
              {t("projects.tasks.workbench.newFile")}
            </Button>
            <Button
              size="small"
              onClick={() => {
                setDestination(directory === "." ? "" : `${directory}/`);
                setOperation("folder");
              }}
            >
              {t("projects.tasks.workbench.newFolder")}
            </Button>
            <Button
              size="small"
              icon={<Upload size={14} />}
              loading={busy}
              onClick={() => uploadRef.current?.click()}
            >
              {t("projects.tasks.workbench.upload")}
            </Button>
          </>
        )}
        <input
          hidden
          type="file"
          ref={uploadRef}
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file)
              void run(() =>
                taskWorkbenchApi.upload(
                  scope.task_id,
                  directory,
                  file,
                  scope.scope_revision,
                ),
              );
          }}
        />
      </Space>
      <div style={{ display: "flex", gap: 16, minHeight: 400 }}>
        <div
          style={{
            width: 230,
            flexShrink: 0,
            maxHeight: 600,
            overflowY: "auto",
            borderRight: "1px solid var(--fn-border)",
            paddingRight: 8,
          }}
        >
          {loading ? (
            <Spin />
          ) : !nodes.length ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            nodes.map((node) => (
              <div
                key={node.path}
                style={{ display: "flex", alignItems: "center" }}
              >
                <Button
                  type={selected?.path === node.path ? "primary" : "text"}
                  block
                  style={{
                    justifyContent: "flex-start",
                    marginBottom: 3,
                    minWidth: 0,
                    flex: 1,
                    overflow: "hidden",
                  }}
                  icon={node.is_dir ? <Folder size={15} /> : <File size={15} />}
                  onClick={() =>
                    leave(() => {
                      if (node.is_dir) {
                        setSelected(null);
                        setDirectory(node.path);
                      } else setSelected(node);
                    })
                  }
                >
                  {node.name}
                </Button>
                {writable && (
                  <Dropdown
                    menu={{
                      items: [
                        {
                          key: "move",
                          label: t("projects.tasks.workbench.move"),
                        },
                        {
                          key: "delete",
                          label: t("projects.tasks.workbench.delete"),
                          danger: true,
                        },
                      ],
                      onClick: ({ key }) =>
                        leave(() => {
                          setSelected(node);
                          setDestination(node.path);
                          setOperation(key as "move" | "delete");
                        }),
                    }}
                    trigger={["click"]}
                  >
                    <Button
                      size="small"
                      aria-label={t("projects.tasks.workbench.fileActions", {
                        path: node.name,
                      })}
                    >
                      …
                    </Button>
                  </Dropdown>
                )}
              </div>
            ))
          )}
        </div>
        <div
          style={{
            flex: 1,
            minWidth: 0,
            height: 600,
            display: "flex",
            flexDirection: "column",
          }}
        >
          {selected ? (
            <>
              <Space wrap style={{ marginBottom: 10 }}>
                <Typography.Text>{selected.path}</Typography.Text>
                <Button
                  size="small"
                  onClick={() => leave(() => setSelected({ ...selected }))}
                >
                  {t("projects.tasks.workbench.reloadFile")}
                </Button>
                <Button
                  size="small"
                  icon={<Download size={14} />}
                  disabled={selected.is_dir}
                  onClick={() =>
                    void run(async () => {
                      const blob = await taskWorkbenchApi.download(
                        scope.task_id,
                        selected.path,
                        scope.scope_revision,
                      );
                      const url = URL.createObjectURL(blob);
                      const link = document.createElement("a");
                      link.href = url;
                      link.download = selected.name;
                      link.click();
                      setTimeout(() => URL.revokeObjectURL(url), 1000);
                    })
                  }
                >
                  {t("projects.tasks.workbench.download")}
                </Button>
                {writable && (
                  <>
                    <Button
                      size="small"
                      disabled={!editable || fileLoading || busy}
                      onClick={() => setEditing((v) => !v)}
                    >
                      {t(
                        editing
                          ? "projects.tasks.workbench.preview"
                          : "projects.tasks.workbench.edit",
                      )}
                    </Button>
                    <Button
                      size="small"
                      icon={<Save size={14} />}
                      disabled={!editable || !dirty || fileLoading}
                      loading={busy}
                      onClick={() =>
                        void run(async () => {
                          const file = await taskWorkbenchApi.save(
                            scope.task_id,
                            selected.path,
                            text,
                            scope.scope_revision,
                            fileRevision,
                          );
                          setSaved(file.content);
                          setFileRevision(file.revision);
                          setText((current) =>
                            current === text ? file.content : current,
                          );
                        })
                      }
                    >
                      {t("projects.save")}
                    </Button>
                    <Button
                      size="small"
                      disabled={dirty || busy}
                      onClick={() => {
                        setDestination(selected.path);
                        setOperation("move");
                      }}
                    >
                      {t("projects.tasks.workbench.move")}
                    </Button>
                    <Button
                      size="small"
                      danger
                      disabled={dirty || busy}
                      onClick={() => setOperation("delete")}
                    >
                      {t("projects.tasks.workbench.delete")}
                    </Button>
                  </>
                )}
              </Space>
              {!selected.is_dir && (
                <FileViewer
                  key={selected.path}
                  path={selected.path}
                  fetchUrl={taskWorkbenchApi.mediaPath(
                    scope.task_id,
                    selected.path,
                    scope.scope_revision,
                  )}
                  editMode={editing && writable}
                  value={text}
                  onChange={setText}
                  fileLoading={fileLoading}
                  refreshToken={version}
                />
              )}
            </>
          ) : (
            <Empty description={t("projects.tasks.workbench.selectFile")} />
          )}
        </div>
      </div>
      <Modal
        open={!!operation}
        title={t(
          `projects.tasks.workbench.${
            operation === "file"
              ? "newFile"
              : operation === "folder"
              ? "newFolder"
              : operation === "move"
              ? "move"
              : "delete"
          }`,
        )}
        onCancel={() => {
          if (!busy) setOperation(null);
        }}
        onOk={() => void mutate()}
        confirmLoading={busy}
        okButtonProps={{
          danger: operation === "delete",
          disabled: operation !== "delete" && !destination.trim(),
        }}
        cancelText={t("projects.cancel")}
      >
        {operation === "delete" ? (
          <Typography.Paragraph>
            {t("projects.tasks.workbench.deleteConfirm", {
              path: selected?.path,
            })}
          </Typography.Paragraph>
        ) : (
          <Input
            aria-label={t("projects.tasks.workbench.path")}
            value={destination}
            onChange={(event) => setDestination(event.target.value)}
          />
        )}
      </Modal>
    </Space>
  );
}
