import { render, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import {
  projectTasksApi,
  type TaskInputAttachment,
} from "../../api/modules/projects";
import TaskMedia, { taskMediaKind } from "./TaskMedia";
vi.mock("../../api/modules/projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/modules/projects")>()),
  projectTasksApi: { attachmentPreview: vi.fn() },
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
const attachment = {
  attachment_id: "audio",
  name: "voice.mp3",
  media_type: "audio/mpeg",
  preview_url: "/api/project-tasks/attachments/audio",
} as TaskInputAttachment;
beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:authenticated-media"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  vi.mocked(projectTasksApi.attachmentPreview).mockResolvedValue(
    new Blob(["audio"], { type: "audio/mpeg" }),
  );
});
it("plays Task audio from authenticated blobs and releases it when leaving", async () => {
  const view = render(<TaskMedia attachment={attachment} />);
  await waitFor(() =>
    expect(view.container.querySelector("audio")).toHaveAttribute(
      "src",
      "blob:authenticated-media",
    ),
  );
  expect(projectTasksApi.attachmentPreview).toHaveBeenCalledExactlyOnceWith(
    "audio",
  );
  view.rerender(<TaskMedia attachment={{ ...attachment }} />);
  expect(projectTasksApi.attachmentPreview).toHaveBeenCalledTimes(1);
  view.unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:authenticated-media");
});
it("recognizes video without mistaking generic files for media", () => {
  expect(taskMediaKind("application/octet-stream", "clip.mp4")).toBe("video");
  expect(taskMediaKind("application/pdf", "brief.pdf")).toBeNull();
});
