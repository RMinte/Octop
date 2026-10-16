import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  projectTasksApi,
  type TaskInputAttachment,
} from "../../api/modules/projects";
import { apiErrorMessage } from "../../utils/apiError";

const IMAGE_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/gif",
]);
export const MAX_TASK_IMAGES = 9;
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;

export function isTaskImage(mediaType: string) {
  return IMAGE_TYPES.has(mediaType);
}

export interface TaskDraftImage {
  id: string;
  name: string;
  isImage: boolean;
  url: string;
  controller: AbortController;
  attachment?: TaskInputAttachment;
  error?: string;
}

export function useTaskImages(scope: string) {
  const { t } = useTranslation();
  const [items, setItems] = useState<TaskDraftImage[]>([]);
  const [error, setError] = useState("");
  const itemsRef = useRef(items);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const generation = useRef(0);

  const clear = useCallback(() => {
    generation.current += 1;
    for (const item of itemsRef.current) {
      item.controller.abort();
      URL.revokeObjectURL(item.url);
    }
    itemsRef.current = [];
    setItems([]);
    setError("");
  }, []);

  useEffect(() => {
    clear();
    return () => {
      generation.current += 1;
      for (const item of itemsRef.current) {
        item.controller.abort();
        URL.revokeObjectURL(item.url);
      }
      itemsRef.current = [];
    };
  }, [scope, clear]);

  const add = (files: File[]) => {
    if (!files.length) return;
    let validationError = "";
    const added: { item: TaskDraftImage; file: File }[] = [];
    for (const file of files) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        validationError = t("projects.tasks.imageSize");
        continue;
      }
      if (itemsRef.current.length + added.length >= MAX_TASK_IMAGES) {
        validationError = t("projects.tasks.imageCount");
        break;
      }
      added.push({
        file,
        item: {
          id: crypto.randomUUID(),
          name: file.name,
          isImage: isTaskImage(file.type),
          url: URL.createObjectURL(file),
          controller: new AbortController(),
        },
      });
    }
    setError(validationError);
    itemsRef.current = [...itemsRef.current, ...added.map(({ item }) => item)];
    setItems(itemsRef.current);
    const currentGeneration = generation.current;
    const currentScope = scope;
    const update = (id: string, changes: Partial<TaskDraftImage>) => {
      if (
        generation.current !== currentGeneration ||
        scopeRef.current !== currentScope
      )
        return;
      itemsRef.current = itemsRef.current.map((item) =>
        item.id === id && !item.controller.signal.aborted
          ? { ...item, ...changes }
          : item,
      );
      setItems(itemsRef.current);
    };
    for (const { item, file } of added) {
      void projectTasksApi
        .uploadAttachment(file, item.controller.signal)
        .then((attachment) =>
          update(item.id, {
            attachment,
            isImage: isTaskImage(attachment.media_type),
          }),
        )
        .catch((err) =>
          update(item.id, {
            error: apiErrorMessage(
              err,
              t("projects.tasks.imageUploadFailed"),
              t,
            ),
          }),
        );
    }
  };

  const remove = (id: string) => {
    const item = itemsRef.current.find((entry) => entry.id === id);
    if (!item) return;
    item.controller.abort();
    URL.revokeObjectURL(item.url);
    itemsRef.current = itemsRef.current.filter((entry) => entry.id !== id);
    setItems(itemsRef.current);
  };

  return {
    items,
    error,
    add,
    remove,
    clear,
    uploading: items.some((item) => !item.attachment && !item.error),
    blocked: items.some((item) => !item.attachment),
    attachmentIDs: items.flatMap((item) =>
      item.attachment ? [item.attachment.attachment_id] : [],
    ),
  };
}
