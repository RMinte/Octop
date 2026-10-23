import { useCallback } from "react";
import type {
  TaskArtifact,
  TaskInputAttachment,
} from "../../api/modules/projects";
import { useTaskHost } from "../../context/TaskHostContext";
import { ChatMediaPlayer } from "../Chat/components/ChatMediaPlayer";

export function taskMediaKind(
  type: string,
  name: string,
): "audio" | "video" | null {
  if (type.startsWith("audio/")) return "audio";
  if (type.startsWith("video/")) return "video";
  if (/\.(mp3|m4a|wav|ogg|oga|aac|flac|opus)$/i.test(name)) return "audio";
  if (/\.(mp4|webm|mov|m4v|ogv)$/i.test(name)) return "video";
  return null;
}
export default function TaskMedia({
  attachment,
  artifact,
  taskID,
}: {
  attachment?: TaskInputAttachment;
  artifact?: TaskArtifact;
  taskID?: string;
}) {
  const { projectTasksApi } = useTaskHost();
  const item = attachment ?? artifact;
  const attachmentID = attachment?.attachment_id;
  const artifactID = artifact?.artifact_id;
  const fetchBlob = useCallback(
    () =>
      attachmentID
        ? projectTasksApi.attachmentPreview(attachmentID)
        : projectTasksApi.downloadArtifact(taskID!, artifactID!),
    [attachmentID, artifactID, taskID, projectTasksApi],
  );
  if (!item) return null;
  const kind = taskMediaKind(item.media_type, item.name);
  if (!kind) return null;
  return (
    <ChatMediaPlayer
      key={attachment?.attachment_id ?? artifact?.artifact_id}
      url={attachment?.preview_url ?? artifact!.download_url}
      filename={item.name}
      mediaType={item.media_type}
      kind={kind}
      fetchBlob={fetchBlob}
    />
  );
}
