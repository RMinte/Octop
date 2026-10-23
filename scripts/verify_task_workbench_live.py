"""Opt-in real UI acceptance against an isolated Octop session.

Usage: uv run --no-sync python scripts/verify_task_workbench_live.py SESSION_JSON
The session must point at a disposable service/profile/project. This does not
attach to a user's browser or read the normal Octop home.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright

FIXTURE = b"""<!doctype html><meta charset=utf-8><title>Octop acceptance playground</title>
<style>body{font:24px sans-serif;padding:40px}input,button{font:24px sans-serif;margin:12px 0;padding:10px;display:block}</style>
<h1>Octop acceptance playground</h1><label>Proof value<input id=proof aria-label="Proof value"></label>
<button id=save onclick="document.querySelector('#result').textContent='Saved: '+document.querySelector('#proof').value">Save proof</button>
<p id=result>Waiting for proof</p>"""


def sample_pdf() -> bytes:
    content = b"BT /F1 24 Tf 40 120 Td (Real Task PDF preview proof) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 500 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return (
        result
        + b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF\n"
    )


async def run(session_path: Path, only: set[str] | None = None) -> int:
    cfg = json.loads(session_path.read_text())
    root = Path(cfg["root"]).resolve()
    if not str(root).startswith(("/private/tmp/", "/tmp/")):
        raise ValueError("A disposable temporary session is required")
    evidence = root / "evidence-ui"
    evidence.mkdir(exist_ok=True)
    project_dir = Path(cfg["project_directory"]).resolve()
    if not project_dir.is_relative_to(root):
        raise ValueError("Project must be inside the disposable session")
    base = cfg["base_url"].rstrip("/")
    if urlsplit(base).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Local acceptance service required")
    locale = json.loads(
        (Path(__file__).resolve().parents[1] / "dashboard/src/locales/zh.json").read_text()
    )
    report: dict = {"base_url": base, "checks": [], "started_at": time.time()}
    secrets = [cfg.get(k, "") for k in ("token", "password", "username")]

    def clean(value: object) -> str:
        result = str(value)
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[redacted]")
        return re.sub(r"([?&](?:token|access_token)=)[^&\s\"']+", r"\1[redacted]", result)

    def tr(key: str) -> str:
        value = locale
        for part in key.split("."):
            value = value[part]
        return value

    def wb(key: str) -> str:
        return tr("projects.tasks.workbench." + key)

    def label(text: str):
        return re.compile("^" + r"\s*".join(re.escape(c) for c in text if not c.isspace()) + "$")

    def save_report() -> None:
        (evidence / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    async def fixture(reader, writer):
        await reader.read(8192)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\nContent-Length: "
            + str(len(FIXTURE)).encode()
            + b"\r\n\r\n"
            + FIXTURE
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(fixture, "127.0.0.1", 0)
    fixture_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    report["fixture_url"] = fixture_url
    failures = 0
    async with httpx.AsyncClient(
        base_url=base, headers={"Authorization": "Bearer " + cfg["token"]}, timeout=60
    ) as client:

        async def api(method: str, path: str, **kwargs):
            response = await client.request(method, "/api" + path, **kwargs)
            if response.is_error:
                try:
                    body = response.json()
                    code = body.get("code") or body.get("error", {}).get("code")
                except Exception:
                    code = "unparseable"
                raise RuntimeError(
                    f"{method} {path.split('?')[0]}: HTTP {response.status_code}, code={code}"
                )
            return response.json() if response.content else None

        state_path = evidence / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
        else:
            project = await api(
                "POST",
                "/projects",
                headers={"Idempotency-Key": uuid.uuid4().hex},
                json={
                    "name": "Real workbench acceptance " + uuid.uuid4().hex[:6],
                    "directory": str(project_dir),
                    "memory_enabled": False,
                },
            )
            task = await api(
                "POST",
                f"/projects/{project['project_id']}/tasks",
                json={
                    "request_id": uuid.uuid4().hex,
                    "prompt": "Reply READY only. Do not use tools.",
                    "selected_expert_id": None,
                    "action_mode": "craft",
                    "web_search_enabled": False,
                    "options": {"model_ref": cfg["model_ref"]},
                },
            )
            state = {"project_id": project["project_id"], "task_id": task["task_id"]}
            state_path.write_text(json.dumps(state))
        task_id = state["task_id"]
        report.update(state)

        async def wait_idle(page=None, timeout=180, after_turn_id=None):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                task = await api("GET", f"/project-tasks/{task_id}")
                if after_turn_id and task["turns"][-1]["turn_id"] == after_turn_id:
                    await asyncio.sleep(0.2)
                    continue
                if not task["active_turn_id"]:
                    if task["turns"][-1]["status"] != "completed":
                        raise RuntimeError("Real model turn ended " + task["turns"][-1]["status"])
                    return task
                if page is not None and task["status"] == "waiting_approval":
                    allow = page.get_by_role(
                        "button", name=label(tr("projects.tasks.allowOnce")), exact=True
                    )
                    if await allow.count():
                        # All prompts below are explicit local fixture/project operations.
                        await allow.first.click()
                await asyncio.sleep(1)
            raise TimeoutError("Real model turn did not finish within acceptance deadline")

        await wait_idle()
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                str(root / "ui-profile"),
                executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                headless=True,
                viewport={"width": 1440, "height": 1000},
                locale="zh-CN",
                args=["--no-first-run", "--no-default-browser-check"],
            )
            await context.add_init_script(
                "sessionStorage.setItem('auth_token', "
                + json.dumps(cfg["token"])
                + "); localStorage.setItem('octop:ui-locale','zh');"
            )
            page = await context.new_page()
            page.set_default_timeout(20000)
            diagnostics: list[dict] = []
            page.on(
                "pageerror",
                lambda error: diagnostics.append(
                    {"kind": "pageerror", "message": clean(error)[:500]}
                ),
            )
            page.on(
                "requestfailed",
                lambda request: diagnostics.append(
                    {
                        "kind": "requestfailed",
                        "url": clean(request.url),
                        "failure": clean(request.failure),
                    }
                ),
            )
            frames = []

            def websocket(ws):
                if "/browser-stream/" in ws.url:

                    def received(payload):
                        try:
                            data = json.loads(payload)
                            if data.get("type") == "frame":
                                frames.append(time.monotonic())
                        except Exception:
                            pass

                    ws.on("framereceived", received)

            page.on("websocket", websocket)
            await page.goto(base + f"/conversations/{task_id}", wait_until="domcontentloaded")
            workbench_button = page.locator(".page-content").get_by_role(
                "button", name=label(wb("title")), exact=True
            )
            await workbench_button.wait_for()

            async def check(name, action):
                nonlocal failures
                if only and name not in only:
                    return
                entry = {"name": name, "started_at": time.time()}
                print("START " + name, flush=True)
                try:
                    detail = await action()
                    entry.update(status="passed", detail=detail)
                    print("PASS " + name, flush=True)
                except Exception as error:
                    failures += 1
                    entry.update(status="failed", error=clean(error)[:1400])
                    print(
                        "FAIL "
                        + name
                        + ": "
                        + (clean(error).splitlines() or [type(error).__name__])[0][:200],
                        flush=True,
                    )
                try:
                    path = evidence / (name + ".png")
                    await page.screenshot(path=str(path), full_page=True, timeout=10000)
                    entry["screenshot"] = str(path)
                except Exception:
                    pass
                report["checks"].append(entry)
                report["diagnostics"] = diagnostics[-30:]
                save_report()

            async def open_workbench(tab):
                drawer = page.locator(".octop-drawer-content")
                if not await drawer.is_visible():
                    await workbench_button.click()
                await drawer.get_by_role("tab", name=label(wb(tab)), exact=True).click()
                return drawer

            async def close_workbench():
                drawer = page.locator(".octop-drawer-content")
                if await drawer.is_visible():
                    await drawer.locator(".octop-drawer-close").click()
                    await drawer.wait_for(state="hidden")

            async def files():
                drawer = await open_workbench("files")
                if not (project_dir / "ui-note.txt").exists():
                    await drawer.get_by_role(
                        "button", name=label(wb("newFile")), exact=True
                    ).click()
                    await page.get_by_role("textbox", name=label(wb("path")), exact=True).fill(
                        "ui-note.txt"
                    )
                    await page.get_by_role("button", name=label("确定")).click()
                await drawer.get_by_role("button", name="ui-note.txt", exact=True).click()
                await drawer.get_by_role("button", name=label(wb("edit")), exact=True).click()
                editor = drawer.locator(".monaco-editor textarea")
                await editor.wait_for(timeout=45000)
                await drawer.locator(".monaco-editor").click(position={"x": 100, "y": 40})
                await page.keyboard.press("ControlOrMeta+A")
                edited_text = "Saved by the real Task file editor: " + uuid.uuid4().hex[:8] + "\n"
                await page.keyboard.type(edited_text, delay=20)
                await drawer.get_by_role(
                    "button", name=label(tr("projects.save")), exact=True
                ).click()
                await asyncio.sleep(0.4)
                assert (project_dir / "ui-note.txt").read_text() == edited_text
                png = evidence / "sample.png"
                png.write_bytes(
                    base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO0kAAAAASUVORK5CYII="
                    )
                )
                await drawer.locator('input[type="file"]').set_input_files(str(png))
                await drawer.get_by_role("button", name="sample.png", exact=True).click()
                image = drawer.locator("img").last
                await image.wait_for()
                await page.wait_for_function(
                    "el => el.complete && el.naturalWidth > 0", arg=await image.element_handle()
                )
                assert (project_dir / "sample.png").is_file()
                pdf = evidence / "sample.pdf"
                pdf.write_bytes(sample_pdf())
                await drawer.locator('input[type="file"]').set_input_files(str(pdf))
                await drawer.get_by_role("button", name="sample.pdf", exact=True).click()
                await drawer.locator(".react-pdf__Page__canvas").wait_for(timeout=45000)
                return {
                    "saved_text": True,
                    "uploaded_image_preview": True,
                    "uploaded_pdf_preview": True,
                }

            await check("files-editor-upload-preview", files)

            async def terminal():
                drawer = await open_workbench("terminal")
                (project_dir / "pty-proof.txt").unlink(missing_ok=True)
                start = drawer.get_by_role("button", name=label(wb("openTerminal")), exact=True)
                if await start.is_enabled():
                    await start.click()
                reconnect = drawer.get_by_role("button", name=label(wb("reconnect")), exact=True)
                for _ in range(100):
                    if await reconnect.is_disabled():
                        break
                    await asyncio.sleep(0.1)
                textbox = drawer.locator(".xterm-helper-textarea")
                await textbox.focus()
                await page.keyboard.type("printf 'real-pty-proof' > pty-proof.txt; pwd\n", delay=15)
                for _ in range(50):
                    if (project_dir / "pty-proof.txt").exists():
                        break
                    await asyncio.sleep(0.1)
                assert (project_dir / "pty-proof.txt").read_text() == "real-pty-proof"
                await close_workbench()
                drawer = await open_workbench("terminal")
                await drawer.locator(".xterm-helper-textarea").wait_for()
                await page.wait_for_function(
                    "() => document.querySelector('.xterm-screen').textContent.includes('real-pty-proof')"
                )
                await drawer.get_by_role(
                    "button", name=label(wb("endTerminal")), exact=True
                ).click()
                await page.get_by_role("button", name=label("确定")).click()
                for _ in range(100):
                    if await drawer.get_by_role(
                        "button", name=label(wb("openTerminal")), exact=True
                    ).is_enabled():
                        break
                    await asyncio.sleep(0.1)
                return {
                    "project_cwd": str(project_dir),
                    "reconnected_history": True,
                    "explicitly_ended": True,
                }

            await check("terminal-real-pty-reconnect", terminal)

            async def browser():
                nonlocal task_page, cdp_browser
                drawer = await open_workbench("browser")
                assert not await drawer.get_by_role(
                    "button", name=label(tr("browserWorkspace.openBrowserWindow")), exact=True
                ).count()
                address = drawer.get_by_placeholder(tr("browserViewer.urlPlaceholder"))
                await address.fill(fixture_url)
                await address.press("Enter")
                for _ in range(100):
                    if frames:
                        break
                    await asyncio.sleep(0.2)
                assert frames, "No actual Chrome screencast frame received"
                takeover = drawer.get_by_role(
                    "button", name=label(tr("browserWorkspace.takeover")), exact=True
                )
                if await takeover.count():
                    await takeover.click()
                ports = json.loads(
                    (
                        Path(cfg["home"]) / "browser-profiles/.octop-browser-profiles.json"
                    ).read_text()
                )
                assert len(ports) == 1, "Only the isolated Task browser may be inspected"
                cdp_browser = await pw.chromium.connect_over_cdp(
                    "http://127.0.0.1:" + str(next(iter(ports.values())))
                )
                task_pages = [p for ctx in cdp_browser.contexts for p in ctx.pages]
                task_page = next((p for p in task_pages if p.url == fixture_url), task_pages[-1])
                await task_page.wait_for_url(fixture_url)
                assert await task_page.title() == "Octop acceptance playground"
                return {"actual_screencast_frames": len(frames), "temporary_fixture": fixture_url}

            task_page = None
            cdp_browser = None
            applied_skill_name = None
            await check("browser-real-chrome-stream", browser)

            async def recording():
                nonlocal applied_skill_name
                drawer = await open_workbench("skillRecording")
                await drawer.get_by_role("textbox", name=label(wb("skillName")), exact=True).fill(
                    "acceptance-skill"
                )
                await drawer.get_by_role(
                    "button", name=label(wb("startRecording")), exact=True
                ).click()
                await drawer.get_by_role(
                    "button", name=label(wb("stopRecording")), exact=True
                ).wait_for(timeout=45000)
                await open_workbench("browser")
                assert task_page is not None
                canvas = drawer.get_by_role(
                    "tabpanel", name=label(wb("browser")), exact=True
                ).locator("canvas")
                rect = await canvas.bounding_box()
                assert rect
                dimensions = await canvas.evaluate("el => ({width:el.width,height:el.height})")

                async def click_remote(selector):
                    box = await task_page.locator(selector).bounding_box()
                    assert box
                    await canvas.click(
                        position={
                            "x": (box["x"] + box["width"] / 2)
                            * rect["width"]
                            / dimensions["width"],
                            "y": (box["y"] + box["height"] / 2)
                            * rect["height"]
                            / dimensions["height"],
                        }
                    )

                # DOM is observed through this Task's isolated CDP port; all
                # input still travels through the real dashboard canvas/WS.
                await click_remote("#proof")
                await page.keyboard.type("ui-browser-proof", delay=60)
                await click_remote("#save")
                await task_page.wait_for_function(
                    "document.querySelector('#result').textContent === 'Saved: ui-browser-proof'"
                )
                await asyncio.sleep(1)
                await open_workbench("skillRecording")
                await drawer.get_by_role(
                    "button", name=label(wb("stopRecording")), exact=True
                ).click()
                skill = drawer.get_by_role("textbox", name=label(wb("skillContent")), exact=True)
                await skill.wait_for(timeout=60000)
                content = await skill.input_value()
                assert len(content) > 50
                applied_skill_name = await drawer.get_by_role(
                    "textbox", name=label(wb("skillName")), exact=True
                ).input_value()
                await drawer.get_by_role("button", name=label(wb("applySkill")), exact=True).click()
                await drawer.get_by_role(
                    "button", name=label(wb("skillApplied")), exact=True
                ).wait_for()
                assert (
                    project_dir / ".octop/workbench/skills" / applied_skill_name / "SKILL.md"
                ).read_text() == content
                return {"generated_skill_characters": len(content), "applied_to_project": True}

            await check("record-browser-generate-apply", recording)

            async def real_model():
                nonlocal applied_skill_name
                if applied_skill_name is None:
                    drawer = await open_workbench("skillRecording")
                    await drawer.get_by_role(
                        "textbox", name=label(wb("skillContent")), exact=True
                    ).wait_for()
                    applied_skill_name = await drawer.get_by_role(
                        "textbox", name=label(wb("skillName")), exact=True
                    ).input_value()
                await open_workbench("browser")
                back = page.get_by_role(
                    "button", name=label(tr("browserWorkspace.returnToAgent")), exact=True
                )
                if await back.count():
                    await back.click()
                await close_workbench()
                composer = page.get_by_role(
                    "textbox", name=label(tr("projects.tasks.message")), exact=True
                )
                before = await api("GET", f"/project-tasks/{task_id}")
                proof = "REAL_MODEL_FILE_PROOF_" + uuid.uuid4().hex[:12]
                await composer.fill(
                    "Use write_file with file_path exactly "
                    + str(project_dir / "model-proof.txt")
                    + " to create a file with exactly "
                    + proof
                    + ", "
                    "then read_file with the same absolute file_path to verify it. Use browser_use to open "
                    + fixture_url
                    + " and inspect its visible DOM/title. "
                    "Only access this local fixture and the current project. Report the exact page title."
                )
                await page.get_by_role(
                    "button", name=label(tr("projects.tasks.send")), exact=True
                ).click()
                await asyncio.sleep(1)
                task = await wait_idle(
                    page, timeout=240, after_turn_id=before["turns"][-1]["turn_id"]
                )
                assert (project_dir / "model-proof.txt").read_text().strip() == proof
                blocks = task["turns"][-1]["content_blocks"]
                tools = [b.get("tool_name") for b in blocks if b["type"] == "tool"]
                texts = "\n".join(str(b.get("text", "")) for b in blocks)
                assert {"write_file", "read_file", "browser_use"}.issubset(set(tools)), (
                    "Actual model did not call every required tool"
                )
                assert all(b["status"] == "completed" for b in blocks if b["type"] == "tool")
                assert "Octop acceptance playground" in texts
                snapshot_path = (
                    Path(cfg["home"])
                    / "project-tasks"
                    / task_id
                    / task["turns"][-1]["turn_id"]
                    / "resources/skills.json"
                )
                snapshot = json.loads(snapshot_path.read_text())
                skill = next(
                    item for item in snapshot["skills"] if item["slug"] == applied_skill_name
                )
                skill_content = base64.b64decode(
                    next(item for item in skill["files"] if item["path"] == "SKILL.md")["base64"]
                )
                assert (
                    skill_content
                    == (
                        project_dir / ".octop/workbench/skills" / applied_skill_name / "SKILL.md"
                    ).read_bytes()
                )
                drawer = await open_workbench("tools")
                await drawer.get_by_role(
                    "combobox", name=label(wb("toolCall")), exact=True
                ).wait_for()
                await drawer.get_by_text(re.compile("Octop acceptance playground")).last.wait_for()
                return {
                    "actual_model_tools": tools,
                    "verified_file": True,
                    "reported_fixture_title": True,
                    "applied_skill_in_next_turn_snapshot": True,
                }

            await check("actual-model-files-browser-tool-ui", real_model)

            async def saved_recording():
                drawer = await open_workbench("skillRecording")
                skill = drawer.get_by_role("textbox", name=label(wb("skillContent")), exact=True)
                await skill.wait_for()
                content = await skill.input_value()
                name = await drawer.get_by_role(
                    "textbox", name=label(wb("skillName")), exact=True
                ).input_value()
                assert (
                    content
                    == (project_dir / ".octop/workbench/skills" / name / "SKILL.md").read_text()
                )
                task = await api("GET", f"/project-tasks/{task_id}")
                turn_id = task["turns"][-1]["turn_id"]
                snapshot = json.loads(
                    (
                        Path(cfg["home"])
                        / "project-tasks"
                        / task_id
                        / turn_id
                        / "resources/skills.json"
                    ).read_text()
                )
                item = next(item for item in snapshot["skills"] if item["slug"] == name)
                saved = next(item for item in item["files"] if item["path"] == "SKILL.md")
                assert base64.b64decode(saved["base64"]).decode() == content
                scope = await api("GET", f"/project-tasks/{task_id}/workbench")
                status = await api(
                    "GET",
                    f"/project-tasks/{task_id}/browser/record-replay/status",
                    params={"expected_scope_revision": scope["scope_revision"]},
                )
                manifest_path = (
                    root / "recordings/recordings" / status["latestRecordingId"] / "manifest.json"
                )
                manifest = json.loads(manifest_path.read_text())
                assert manifest["stats"]["events"] > 0 and manifest["stats"]["steps"] >= 3
                await open_workbench("tools")
                await drawer.get_by_role(
                    "combobox", name=label(wb("toolCall")), exact=True
                ).wait_for()
                await drawer.get_by_text(re.compile("Octop acceptance playground")).last.wait_for()
                await page.screenshot(
                    path=str(evidence / "actual-rendered-tool-result.png"), full_page=True
                )
                return {
                    "skill_name": name,
                    "generated_skill_characters": len(content),
                    "recorded_events": manifest["stats"]["events"],
                    "recorded_steps": manifest["stats"]["steps"],
                    "applied_file_and_latest_turn_snapshot_match": True,
                    "tool_result_rendered": True,
                }

            await check("saved-recording-and-model-snapshot", saved_recording)
            await context.close()
            if cdp_browser:
                await cdp_browser.close()
    server.close()
    await server.wait_closed()
    report.update(finished_at=time.time(), failures=failures)
    save_report()
    print("REPORT " + str(evidence / "report.json"), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--only", help="Comma-separated check names; defaults to every check")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(run(args.session, set(args.only.split(",")) if args.only else None))
    )
