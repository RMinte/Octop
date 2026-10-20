"""Tool intent, not an early end marker, decides whether prose is an answer."""

from octop.infra.projects.response_text import TaskResponseText


def token(text, *, step_id=None):
    return {"type": "token", "content": text, "block_key": "response:text", "step_id": step_id}


def test_answer_waits_for_confirmed_response_and_preserves_interleaved_summary():
    text = TaskResponseText()
    first, last = token("First "), token("answer.")
    summary = {"type": "reasoning_summary", "content": "Public summary", "block_key": "summary"}
    assert text.append(first) == []
    assert text.append(summary) == []
    assert text.append(last) == []
    assert text.complete(has_tools=False) == [first, summary, last]


def test_early_message_end_does_not_publish_tool_prose_as_an_answer():
    text = TaskResponseText()
    prose = token("I will first inspect the file.", step_id="original-step")
    boundary = {"type": "message_end"}
    assert text.append(prose) == []
    assert text.append(boundary) == []
    assert text.complete(has_tools=True) == [{**prose, "type": "reasoning"}, boundary]
    assert text.append(token("Tool explanation.")) == [
        {**token("Tool explanation."), "type": "reasoning"}
    ]


def test_executing_plan_prose_is_process_text_even_without_tool_calls():
    text = TaskResponseText(executing=True)
    prose = token("Intermediate planning explanation.")
    assert text.append(prose) == [{**prose, "type": "reasoning"}]
    assert text.complete(has_tools=False) == []


def test_explicitly_tool_free_response_streams_without_waiting_for_completion():
    text = TaskResponseText()
    assert text.begin(executing=False, tools_disabled=True) == []
    answer = token("Actual verified result.")
    assert text.append(answer) == [answer]
    assert text.append({"type": "message_end"}) == [{"type": "message_end"}]
    assert text.complete(has_tools=False) == []
    assert text.begin(executing=False) == []
    prose = token("A later response may still use tools.")
    assert text.append(prose) == []
    assert text.complete(has_tools=True) == [{**prose, "type": "reasoning"}]


def test_new_response_does_not_inherit_tool_classification_or_step_binding():
    text = TaskResponseText()
    prose = token("Preparing work.", step_id="step-a")
    text.append(prose)
    assert text.complete(has_tools=True) == [{**prose, "type": "reasoning"}]
    assert text.begin(executing=False) == []
    answer = token("Actual verified result.")
    assert text.append(answer) == []
    assert text.complete(has_tools=False) == [answer]


def test_unresolved_previous_response_stays_process_text():
    text = TaskResponseText()
    prose = token("Incomplete provider prose.")
    text.append(prose)
    assert text.begin(executing=False) == [{**prose, "type": "reasoning"}]
    answer = token("The next response is final.")
    text.append(answer)
    assert text.complete(has_tools=False) == [answer]
