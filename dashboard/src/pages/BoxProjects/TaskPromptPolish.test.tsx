import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { taskExtrasApi } from "../../api/modules/taskExtras";
import TaskPromptPolish from "./TaskPromptPolish";
vi.mock("../../api/modules/taskExtras", () => ({
  taskExtrasApi: { polish: vi.fn() },
}));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
it("requires review and refuses to replace a draft changed during the model request", async () => {
  let resolve!: (value: { text: string }) => void;
  vi.mocked(taskExtrasApi.polish).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const apply = vi.fn();
  const props = {
    text: "original",
    taskID: "task",
    expertID: null,
    disabled: false,
    onApply: apply,
  };
  const view = render(<TaskPromptPolish {...props} />);
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.polish.title" }),
  );
  expect(taskExtrasApi.polish).toHaveBeenCalledWith({
    text: "original",
    task_id: "task",
    selected_expert_id: null,
    model_ref: undefined,
  });
  view.rerender(<TaskPromptPolish {...props} text="new draft" />);
  await act(async () => resolve({ text: "polished" }));
  expect(
    await screen.findByRole("textbox", {
      name: "projects.tasks.polish.review",
    }),
  ).toHaveValue("polished");
  expect(
    screen.getByRole("button", { name: "projects.tasks.polish.apply" }),
  ).toBeDisabled();
  expect(apply).not.toHaveBeenCalled();
});
it("only applies the reviewed text after explicit acceptance", async () => {
  vi.mocked(taskExtrasApi.polish).mockResolvedValue({ text: "suggested" });
  const apply = vi.fn();
  render(
    <TaskPromptPolish
      text="original"
      expertID={null}
      disabled={false}
      onApply={apply}
    />,
  );
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.polish.title" }),
  );
  const input = await screen.findByRole("textbox", {
    name: "projects.tasks.polish.review",
  });
  fireEvent.change(input, { target: { value: "reviewed" } });
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("button", { name: "projects.tasks.polish.apply" }),
  );
  await waitFor(() => expect(apply).toHaveBeenCalledWith("reviewed"));
});
