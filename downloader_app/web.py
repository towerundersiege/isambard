from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .download_manager import DownloadManager


EXTENSION_DIR = Path(__file__).resolve().parent.parent / "browser_extension"
LOGGER = logging.getLogger("isambard.web")


class QueueRequest(BaseModel):
    title: str
    url: str
    metadata: dict = {}


class BrowserStateRequest(BaseModel):
    page_url: str = ""
    page_title: str = ""
    metadata: dict = {}
    streams: list[dict] = []
    can_go_back: bool = False
    can_go_forward: bool = False


class BrowserCommandRequest(BaseModel):
    command_id: int = 0


def build_app(download_manager: DownloadManager) -> FastAPI:
    app = FastAPI(title="Downloader")
    app.mount("/extension", StaticFiles(directory=EXTENSION_DIR), name="extension")
    app.state.browser_state = {
        "page_url": "",
        "page_title": "",
        "metadata": {},
        "streams": [],
        "can_go_back": False,
        "can_go_forward": False,
    }
    app.state.browser_command = {"id": 0, "action": ""}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        browser_port = os.environ.get("ISAMBARD_BROWSER_PORT", "8766")
        browser_url = os.environ.get("BROWSER_URL", f"http://localhost:{browser_port}")
        browser_user = os.environ.get("BROWSER_USER", "guac")
        browser_password = os.environ.get("BROWSER_PASSWORD", "guac")
        return (
            INDEX_HTML.replace("__BROWSER_URL__", browser_url)
            .replace("__BROWSER_USER__", browser_user)
            .replace("__BROWSER_PASSWORD__", browser_password)
        )

    @app.get("/api/tasks")
    def tasks() -> dict:
        return download_manager.snapshot()

    @app.post("/api/tasks")
    def create_task(request: QueueRequest) -> dict:
        LOGGER.info("api enqueue task title=%s url=%s", request.title, request.url)
        task = download_manager.enqueue(request.title, request.url, request.metadata)
        return task.to_dict()

    @app.post("/api/browser/queue")
    def create_browser_task(request: QueueRequest) -> dict:
        LOGGER.info("browser queue task title=%s url=%s", request.title, request.url)
        task = download_manager.enqueue(request.title, request.url, request.metadata)
        return task.to_dict()

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str) -> dict:
        LOGGER.info("api stop task id=%s", task_id)
        task = download_manager.stop_task(task_id)
        if task is None:
            LOGGER.warning("stop task failed id=%s reason=not_found", task_id)
            return {"ok": False, "error": "task not found"}
        LOGGER.info("task stop result id=%s status=%s", task_id, task.status)
        return {"ok": True, "task": task.to_dict()}

    @app.get("/api/browser/state")
    def browser_state() -> dict:
        return app.state.browser_state

    @app.post("/api/browser/state")
    def update_browser_state(request: BrowserStateRequest) -> dict:
        LOGGER.debug(
            "browser state update page_url=%s streams=%s back=%s forward=%s",
            request.page_url,
            len(request.streams),
            request.can_go_back,
            request.can_go_forward,
        )
        app.state.browser_state = {
            "page_url": request.page_url,
            "page_title": request.page_title,
            "metadata": request.metadata,
            "streams": request.streams[:20],
            "can_go_back": request.can_go_back,
            "can_go_forward": request.can_go_forward,
        }
        return {"ok": True}

    @app.get("/api/browser/command")
    def browser_command() -> dict:
        return app.state.browser_command

    @app.post("/api/browser/command/{action}")
    def issue_browser_command(action: str) -> dict:
        if action not in {"back", "forward", "reload"}:
            LOGGER.warning("unsupported browser command action=%s", action)
            return {"ok": False, "error": "unsupported action"}
        current_id = int(app.state.browser_command.get("id", 0)) + 1
        app.state.browser_command = {"id": current_id, "action": action}
        LOGGER.info("issued browser command id=%s action=%s", current_id, action)
        return {"ok": True, "id": current_id, "action": action}

    @app.post("/api/browser/command/ack")
    def acknowledge_browser_command(request: BrowserCommandRequest) -> dict:
        if int(app.state.browser_command.get("id", 0)) == request.command_id:
            app.state.browser_command = {"id": request.command_id, "action": ""}
            LOGGER.info("acknowledged browser command id=%s", request.command_id)
        return {"ok": True}

    @app.get("/extension.zip")
    def extension_zip() -> StreamingResponse:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in EXTENSION_DIR.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(EXTENSION_DIR))
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="yflix-downloader-extension.zip"'},
        )

    return app


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Isambard</title>
  <style>
    :root {
      --panel: rgba(13, 24, 37, 0.92);
      --panel-2: rgba(18, 31, 46, 0.92);
      --border: rgba(255, 255, 255, 0.08);
      --text: #e8eef7;
      --muted: #8ea7c4;
      --queued: #8b95a7;
      --queued-bg: rgba(139, 149, 167, 0.14);
      --running: #f5c451;
      --running-bg: rgba(245, 196, 81, 0.16);
      --completed: #22c55e;
      --completed-bg: rgba(34, 197, 94, 0.14);
      --failed: #ef4444;
      --failed-bg: rgba(239, 68, 68, 0.14);
      --action: #0f7bff;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      font-family: "SF Pro Display", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(15,123,255,0.22), transparent 28%),
        radial-gradient(circle at top right, rgba(34,197,94,0.14), transparent 22%),
        linear-gradient(180deg, #071019 0%, #0b1521 100%);
    }
    .shell {
      width: calc(100vw - 32px);
      margin: 16px auto;
      padding-bottom: 16px;
    }
    .header {
      margin-bottom: 16px;
    }
    .title {
      margin: 0;
      font-size: clamp(30px, 4vw, 46px);
      font-weight: 800;
      letter-spacing: -0.04em;
    }
    .subtitle {
      margin-top: 8px;
      color: var(--muted);
      max-width: 920px;
    }
    .top-grid {
      display: grid;
      grid-template-columns: minmax(0, 2.15fr) minmax(240px, 0.45fr);
      gap: 16px;
      align-items: stretch;
      margin-bottom: 16px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      overflow: hidden;
      backdrop-filter: blur(18px);
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.28);
    }
    .panel-header {
      padding: 12px 16px;
      min-height: 58px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .panel-title {
      margin: 0;
      font-size: 13px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .pill {
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 999px;
      color: var(--muted);
      padding: 6px 10px;
      font-size: 12px;
      white-space: nowrap;
    }
    .browser-shell {
      min-height: 0;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }
    .browser-body {
      aspect-ratio: 16 / 9;
      min-height: 0;
    }
    .browser-wrap {
      display: grid;
      grid-template-rows: minmax(0, 1fr);
      gap: 0;
      min-height: 0;
      height: 100%;
    }
    .browser-controls {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-left: auto;
      min-height: 40px;
    }
    .browser-btn {
      min-width: 44px;
      height: 40px;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px;
      padding: 0 12px;
      background: rgba(255,255,255,0.06);
      color: var(--text);
      font-size: 20px;
      font-weight: 700;
      line-height: 0;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .browser-btn:hover {
      background: rgba(255,255,255,0.1);
    }
    .browser-btn:disabled {
      opacity: 0.42;
      cursor: default;
      background: rgba(255,255,255,0.03);
      color: rgba(232, 238, 247, 0.52);
    }
    .browser-frame {
      display: block;
      width: 100%;
      height: 100%;
      border: 0;
      background: #0b1118;
    }
    .browser-frame:focus {
      outline: 2px solid rgba(15,123,255,0.9);
      outline-offset: -2px;
    }
    .stream-panel {
      height: 100%;
      max-height: 100%;
      min-height: 0;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }
    .stream-list {
      padding: 14px;
      display: grid;
      gap: 12px;
      height: 100%;
      min-height: 0;
      overflow-y: scroll;
      overflow-x: hidden;
      scrollbar-gutter: stable;
      align-content: start;
    }
    .stream-item {
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 18px;
      padding: 14px;
      min-width: 0;
      width: 100%;
      overflow: hidden;
    }
    .stream-title {
      font-size: 15px;
      font-weight: 700;
      line-height: 1.3;
      margin-bottom: 6px;
      min-width: 0;
    }
    .stream-url {
      color: #abc0dc;
      font-size: 12px;
      line-height: 1.45;
      word-break: break-word;
      overflow-wrap: anywhere;
      margin: 8px 0 12px;
      min-width: 0;
    }
    .stream-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }
    .stream-meta {
      color: var(--muted);
      font-size: 12px;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .queue-btn {
      border: 0;
      border-radius: 999px;
      padding: 10px 14px;
      background: linear-gradient(90deg, rgba(15,123,255,0.95), rgba(57,183,255,0.95));
      color: #fff;
      font-weight: 700;
      cursor: pointer;
      min-width: 44px;
      flex: 0 0 auto;
    }
    .queue-btn[disabled] {
      opacity: 0.9;
      cursor: default;
    }
    .task-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }
    .task-panel {
      min-height: 0;
      height: auto;
    }
    .task-list {
      padding: 14px;
      display: grid;
      gap: 12px;
      height: 100%;
      overflow-y: scroll;
      overflow-x: hidden;
      scrollbar-gutter: stable;
      min-height: 0;
      align-content: start;
    }
    .task-list-wrap {
      display: none;
      min-height: 0;
      overflow: hidden;
    }
    .task-panel[open] .task-list-wrap {
      height: 420px;
      display: block;
    }
    .stream-list,
    .task-list {
      scrollbar-width: auto;
    }
    .task-panel-toggle {
      display: block;
      list-style: none;
      cursor: pointer;
    }
    .task-panel-toggle::-webkit-details-marker {
      display: none;
    }
    .task-panel-toggle .panel-header {
      margin: 0;
      border-bottom: 0;
    }
    .panel-title-row {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }
    .panel-chevron {
      width: 14px;
      height: 14px;
      color: var(--muted);
      flex: 0 0 auto;
      transition: transform 0.18s ease;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .task-panel[open] .panel-chevron {
      transform: rotate(90deg);
    }
    .task {
      border: 1px solid rgba(255,255,255,0.07);
      border-radius: 20px;
      overflow: hidden;
      background: rgba(255,255,255,0.025);
    }
    .task summary {
      list-style: none;
      cursor: pointer;
      padding: 12px 16px;
      display: grid;
      gap: 6px;
    }
    .task summary::-webkit-details-marker { display: none; }
    .task-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 6px;
      align-items: center;
    }
    .task-topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }
    .task-title-row {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      flex: 1 1 auto;
      flex-wrap: wrap;
      line-height: 1;
    }
    .task-title {
      font-size: 16px;
      font-weight: 750;
      line-height: 1;
      min-width: 0;
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0;
      margin: 0;
    }
    .task-title-path {
      color: var(--muted);
      font-size: 12px;
      line-height: 1;
      word-break: break-all;
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0;
      margin: 0;
    }
    .task-inline-size {
      color: var(--muted);
      font-size: 12px;
      line-height: 1;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0;
      margin: 0;
    }
    .task-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .task-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-left: auto;
    }
    .task-stop-btn {
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.05);
      color: var(--muted);
      border-radius: 10px;
      padding: 6px 10px;
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
    }
    .task-stop-btn:hover {
      background: rgba(255,255,255,0.1);
      color: var(--text);
    }
    .stamp {
      color: var(--muted);
      font-size: 12px;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.04);
    }
    .status-chip {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 34px;
      height: 34px;
      border-radius: 999px;
    }
    .status-queued {
      color: #d4dae4;
      background: var(--queued-bg);
      border: 1px solid rgba(139, 149, 167, 0.25);
    }
    .status-running {
      color: #ffe7a0;
      background: var(--running-bg);
      border: 1px solid rgba(245, 196, 81, 0.28);
    }
    .status-completed {
      color: #90efb2;
      background: var(--completed-bg);
      border: 1px solid rgba(34, 197, 94, 0.25);
    }
    .status-failed {
      color: #ffaaa5;
      background: var(--failed-bg);
      border: 1px solid rgba(239, 68, 68, 0.25);
    }
    .status-stopped {
      color: #d4dae4;
      background: var(--queued-bg);
      border: 1px solid rgba(139, 149, 167, 0.25);
    }
    .status-icon {
      width: 14px;
      height: 14px;
      border-radius: 999px;
      display: inline-block;
      position: relative;
      flex: 0 0 auto;
    }
    .status-queued .status-icon {
      background: radial-gradient(circle at 35% 35%, #c8cfdb 0, #a0a9ba 55%, #717c8d 100%);
    }
    .status-completed .status-icon {
      background: radial-gradient(circle at 35% 35%, #9bf2bb 0, #47d679 55%, #15984a 100%);
    }
    .status-failed .status-icon {
      background: radial-gradient(circle at 35% 35%, #ffaca7 0, #f56c64 55%, #bf2b2b 100%);
    }
    .status-stopped .status-icon {
      background: radial-gradient(circle at 35% 35%, #c8cfdb 0, #a0a9ba 55%, #717c8d 100%);
    }
    .status-running .status-icon {
      border: 2px solid rgba(245, 196, 81, 0.25);
      border-top-color: var(--running);
      border-right-color: #fff0b8;
      background: transparent;
      animation: spin 0.9s linear infinite;
    }
    .progress {
      width: 100%;
      height: 10px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255,255,255,0.05);
    }
    .progress > span {
      display: block;
      height: 100%;
      border-radius: 999px;
    }
    .progress.running > span { background: linear-gradient(90deg, #d1a73a, #f5c451); }
    .progress.queued > span { background: linear-gradient(90deg, #7d8798, #a3adbf); }
    .progress.completed > span { background: linear-gradient(90deg, #1ca94f, #4ce27d); }
    .progress.failed > span { background: linear-gradient(90deg, #d93939, #ff6f6f); }
    .task-extra {
      color: var(--muted);
      font-size: 12px;
    }
    .task-body {
      border-top: 1px solid rgba(255,255,255,0.06);
      padding: 10px 16px 14px;
      display: grid;
      gap: 8px;
    }
    .task-url {
      font-size: 12px;
      color: #abc0dc;
      word-break: break-all;
      line-height: 1.45;
    }
    .log-box {
      margin: 0;
      background: rgba(0,0,0,0.26);
      border: 1px solid rgba(255,255,255,0.04);
      border-radius: 14px;
      padding: 12px;
      color: #c4d4e6;
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 11px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow: auto;
      max-height: 300px;
    }
    .empty {
      padding: 22px;
      color: var(--muted);
    }
    .empty-centered {
      min-height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
      text-align: center;
    }
    @keyframes spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    @media (max-width: 1100px) {
      .top-grid {
        grid-template-columns: 1fr;
      }
      .task-grid {
        grid-template-columns: 1fr;
      }
    }
    @media (max-width: 720px) {
      .shell {
        width: calc(100vw - 18px);
        margin: 9px auto;
      }
      .task-head {
        grid-template-columns: 1fr;
      }
      .stream-actions {
        align-items: start;
        flex-direction: column;
      }
      .queue-btn {
        width: 100%;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="header">
      <h1 class="title">Isambard</h1>
    </section>

    <section class="top-grid">
      <section class="browser-wrap">
        <div class="panel browser-shell">
          <div class="panel-header">
            <div class="panel-title-row">
              <h2 class="panel-title">Browser</h2>
            </div>
            <div class="browser-controls" aria-label="Browser navigation">
              <button class="browser-btn" data-browser-action="back" aria-label="Back" title="Back">&#x2039;</button>
              <button class="browser-btn" data-browser-action="forward" aria-label="Forward" title="Forward">&#x203A;</button>
              <button class="browser-btn" data-browser-action="reload" aria-label="Refresh" title="Refresh">&#x21bb;</button>
            </div>
          </div>
          <div class="browser-body">
            <iframe
              id="browser-frame"
              class="browser-frame"
              src="__BROWSER_URL__"
              tabindex="0"
              allow="autoplay; clipboard-read; clipboard-write; fullscreen"
              referrerpolicy="no-referrer"
            ></iframe>
          </div>
        </div>
      </section>

      <section class="panel stream-panel">
        <header class="panel-header">
          <h2 class="panel-title">Streams</h2>
          <span class="pill" id="browser-stream-count">0 detected</span>
        </header>
        <div class="stream-list" id="browser-stream-list"></div>
      </section>
    </section>

    <section class="task-grid">
      <details class="panel task-panel" open>
        <summary class="task-panel-toggle">
          <div class="panel-header">
            <div class="panel-title-row">
              <span class="panel-chevron" aria-hidden="true">&#x203a;</span>
              <h2 class="panel-title">Active</h2>
            </div>
            <span class="pill" id="active-count">0 items</span>
          </div>
        </summary>
        <div class="task-list-wrap">
          <div class="task-list" id="active-task-list"></div>
        </div>
      </details>
      <details class="panel task-panel" id="completed-panel">
        <summary class="task-panel-toggle">
          <div class="panel-header">
            <div class="panel-title-row">
              <span class="panel-chevron" aria-hidden="true">&#x203a;</span>
              <h2 class="panel-title">Completed</h2>
            </div>
            <span class="pill" id="completed-count">0 items</span>
          </div>
        </summary>
        <div class="task-list-wrap">
          <div class="task-list" id="completed-task-list"></div>
        </div>
      </details>
    </section>
  </main>

  <script>
    const browserFrame = document.getElementById("browser-frame");
    let browserHasIntentFocus = false;
    let browserFocusTimer = null;
    function focusBrowserFrame() {
      if (!browserFrame) {
        return;
      }
      browserHasIntentFocus = true;
      browserFrame.focus();
    }
    function scheduleBrowserRefocus() {
      if (browserFocusTimer) {
        clearTimeout(browserFocusTimer);
      }
      browserFocusTimer = setTimeout(() => {
        focusBrowserFrame();
      }, 30);
    }
    if (browserFrame) {
      browserFrame.addEventListener("load", () => {
        focusBrowserFrame();
      });
      browserFrame.addEventListener("mousedown", () => {
        focusBrowserFrame();
      });
      browserFrame.addEventListener("mouseenter", () => {
        scheduleBrowserRefocus();
      });
      browserFrame.addEventListener("click", () => {
        scheduleBrowserRefocus();
      });
    }
    document.querySelector(".browser-shell")?.addEventListener("mousedown", () => {
      scheduleBrowserRefocus();
    });
    document.addEventListener("mousedown", (event) => {
      if (!(event.target instanceof Element)) {
        browserHasIntentFocus = false;
        return;
      }
      if (!event.target.closest(".browser-wrap")) {
        browserHasIntentFocus = false;
      }
    });
    const browserShell = document.querySelector(".browser-shell");
    const streamPanel = document.querySelector(".stream-panel");
    if (browserShell && streamPanel && "ResizeObserver" in window) {
      const observer = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const height = Math.round(entry.contentRect.height);
          if (height > 0) {
            const browserWrap = document.querySelector(".browser-wrap");
            const wrapHeight = browserWrap ? Math.round(browserWrap.getBoundingClientRect().height) : height;
            streamPanel.style.height = `${wrapHeight}px`;
            streamPanel.style.maxHeight = `${wrapHeight}px`;
          }
        }
      });
      observer.observe(document.querySelector(".browser-wrap"));
    }
    document.querySelectorAll("[data-browser-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.dataset.browserAction;
        if (button.disabled) {
          return;
        }
        button.disabled = true;
        try {
          await fetch(`/api/browser/command/${action}`, { method: "POST" });
          scheduleBrowserRefocus();
        } finally {
          setTimeout(() => {
            button.disabled = false;
          }, 400);
        }
      });
    });
    document.addEventListener("keydown", (event) => {
      const scrollingKeys = new Set([" ", "Spacebar", "PageDown", "PageUp", "ArrowDown", "ArrowUp", "Home", "End"]);
      if (!browserHasIntentFocus || !scrollingKeys.has(event.key)) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
    }, true);
    window.addEventListener("focus", () => {
      if (browserHasIntentFocus) {
        scheduleBrowserRefocus();
      }
    });

    function updateBrowserButtons(state) {
      const backButton = document.querySelector('[data-browser-action="back"]');
      const forwardButton = document.querySelector('[data-browser-action="forward"]');
      if (backButton) {
        backButton.disabled = !state.can_go_back;
      }
      if (forwardButton) {
        forwardButton.disabled = !state.can_go_forward;
      }
    }

    function escapeHtml(input) {
      return String(input ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      })[char]);
    }

    function parseTime(value) {
      if (!value) return null;
      const parsed = new Date(value);
      return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    function formatTime(value) {
      const parsed = parseTime(value);
      if (!parsed) return "—";
      return new Intl.DateTimeFormat(undefined, {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
      }).format(parsed);
    }

    function allTasks() {
      return [
        ...(window.__taskState?.running || []),
        ...(window.__taskState?.queued || []),
        ...(window.__taskState?.completed || [])
      ];
    }

    function orderedTaskGroups() {
      const tasks = allTasks();
      const active = [
        ...tasks.filter((task) => task.status === "running"),
        ...tasks.filter((task) => task.status === "queued")
      ];
      const completed = tasks
        .filter((task) => task.status === "completed" || task.status === "failed")
        .sort((a, b) => (parseTime(b.finished_at)?.getTime() || 0) - (parseTime(a.finished_at)?.getTime() || 0));
      return { active, completed };
    }

    function findTaskForStream(url) {
      return allTasks().find((task) => task.url === url) || null;
    }

    function queueButtonLabel(task) {
      if (!task) return "Add to Queue";
      if (task.status === "running") return `${Math.round(task.progress || 0)}%`;
      if (task.status === "completed") return "Successful";
      if (task.status === "failed") return "Retry";
      return "Queued";
    }

    function queueButtonDisabled(task) {
      return !!task && task.status !== "failed";
    }

    async function persistBrowserState(state) {
      await fetch("/api/browser/state", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state)
      });
    }

    function streamCard(stream, metadata, state) {
      const task = findTaskForStream(stream.url);
      const disabled = queueButtonDisabled(task) ? "disabled" : "";
      const title = metadata?.raw_title || state.page_title || "Detected stream";
      const metaLine = metadata?.season && metadata?.episode
        ? `Season ${metadata.season} • Episode ${metadata.episode}`
        : "Queue with yt-dlp";
      return `
        <article class="stream-item">
          <div class="stream-title">${escapeHtml(title)}</div>
          <div class="stream-meta">${escapeHtml(metaLine)}</div>
          <div class="stream-url">${escapeHtml(stream.url)}</div>
          <div class="stream-actions">
            <div class="stream-meta">${escapeHtml(state.page_url || "")}</div>
            <button class="queue-btn" data-stream-url="${encodeURIComponent(stream.url)}" ${disabled} aria-label="Add to queue" title="Add to queue">+</button>
          </div>
        </article>
      `;
    }

    function taskCard(task) {
      const progress = task.status === "completed" ? 100 : Math.max(0, Math.min(100, task.progress || 0));
      const progressClass = escapeHtml(task.status);
      const extra = [task.speed, task.eta ? `ETA ${task.eta}` : ""].filter(Boolean).join(" • ");
      const logs = task.output || task.error || "No logs yet.";
      const timestamps = [];
      if (task.status === "queued") {
        timestamps.push(`Added ${formatTime(task.created_at)}`);
      } else if (task.status === "running") {
        timestamps.push(`Started ${formatTime(task.started_at || task.created_at)}`);
      } else {
        timestamps.push(`Started ${formatTime(task.started_at || task.created_at)}`);
        if (task.finished_at) {
          timestamps.push(`Finished ${formatTime(task.finished_at)}`);
        }
      }
      const outputPath = String(task.output_template || "").replace(/%\\((ext)\\)s/g, "mp4");
      const progressBar = task.status === "running" || task.status === "failed"
        ? `<div class="progress ${progressClass}"><span style="width:${progress}%"></span></div>`
        : "";
      const stopAction = (task.status === "queued" || task.status === "running")
        ? `<button class="task-stop-btn" data-stop-task-id="${escapeHtml(task.id)}">${task.status === "queued" ? "Remove" : "Stop"}</button>`
        : "";
      return `
        <details class="task ${escapeHtml(task.status)}" data-task-id="${escapeHtml(task.id)}">
          <summary>
            <div class="task-head">
              <div class="task-topline">
                <div class="task-title-row">
                  <span class="status-chip status-${escapeHtml(task.status)}" aria-label="${escapeHtml(task.status)}"><span class="status-icon"></span></span>
                  <div class="task-title">${escapeHtml(task.title)}</div>
                  ${outputPath ? `<div class="task-title-path">${escapeHtml(outputPath)}</div>` : ""}
                  ${task.filesize ? `<div class="task-inline-size">${escapeHtml(task.filesize)}</div>` : ""}
                </div>
                <div class="task-meta">
                  ${timestamps.map((label) => `<span class="stamp">${escapeHtml(label)}</span>`).join("")}
                  ${stopAction ? `<span class="task-actions">${stopAction}</span>` : ""}
                </div>
              </div>
              <div>
                ${extra ? `<div class="task-extra">${escapeHtml(extra)}</div>` : ""}
              </div>
            </div>
            ${progressBar}
          </summary>
          <div class="task-body">
            <div class="task-url">${escapeHtml(task.url)}</div>
            <pre class="log-box" data-task-id="${escapeHtml(task.id)}">${escapeHtml(logs)}</pre>
          </div>
        </details>
      `;
    }

    function captureLogScrollState() {
      const state = {};
      document.querySelectorAll(".log-box[data-task-id]").forEach((node) => {
        const maxScroll = Math.max(0, node.scrollHeight - node.clientHeight);
        state[node.dataset.taskId] = {
          top: node.scrollTop,
          follow: maxScroll - node.scrollTop <= 8
        };
      });
      return state;
    }

    function restoreLogScrollState(state) {
      document.querySelectorAll(".log-box[data-task-id]").forEach((node) => {
        const saved = state[node.dataset.taskId];
        if (!saved) {
          return;
        }
        if (saved.follow) {
          node.scrollTop = node.scrollHeight;
          return;
        }
        const maxScroll = Math.max(0, node.scrollHeight - node.clientHeight);
        node.scrollTop = Math.min(saved.top, maxScroll);
      });
    }

    function renderTaskList(listId, tasks, emptyMessage, openTaskId) {
      const list = document.getElementById(listId);
      if (!tasks.length) {
        const emptyClass = listId === "active-task-list" ? "empty empty-centered" : "empty";
        list.innerHTML = `<div class="${emptyClass}">${escapeHtml(emptyMessage)}</div>`;
        return;
      }
      list.innerHTML = tasks.map(taskCard).join("");
      if (openTaskId) {
        const openNode = list.querySelector(`.task[data-task-id="${CSS.escape(openTaskId)}"]`);
        if (openNode) {
          openNode.open = true;
        }
      }
      list.querySelectorAll(".task[data-task-id]").forEach((node) => {
        node.addEventListener("toggle", () => {
          if (!node.open) {
            if (window.__openTaskId === node.dataset.taskId) {
              window.__openTaskId = "";
            }
            return;
          }
          window.__openTaskId = node.dataset.taskId;
          document.querySelectorAll(".task[data-task-id][open]").forEach((other) => {
            if (other !== node) {
              other.open = false;
            }
          });
        });
      });
      list.querySelectorAll(".task-stop-btn[data-stop-task-id]").forEach((button) => {
        button.addEventListener("click", async (event) => {
          event.preventDefault();
          event.stopPropagation();
          const taskId = button.dataset.stopTaskId;
          if (!taskId) {
            return;
          }
          button.disabled = true;
          try {
            await fetch(`/api/tasks/${taskId}/stop`, { method: "POST" });
            refresh();
          } finally {
            button.disabled = false;
          }
        });
      });
    }

    function renderTasks() {
      const groups = orderedTaskGroups();
      const openTaskId = window.__openTaskId || "";
      const logScrollState = captureLogScrollState();
      document.getElementById("active-count").textContent = `${groups.active.length} items`;
      document.getElementById("completed-count").textContent = `${groups.completed.length} items`;
      renderTaskList("active-task-list", groups.active, "No active tasks.", openTaskId);
      renderTaskList("completed-task-list", groups.completed, "No completed tasks yet.", openTaskId);
      restoreLogScrollState(logScrollState);
    }

    async function refreshBrowserState() {
      const response = await fetch("/api/browser/state");
      const state = await response.json();
      window.__browserState = state;
      updateBrowserButtons(state);
      const streams = (state.streams || []).filter((stream) => !findTaskForStream(stream.url));
      document.getElementById("browser-stream-count").textContent = `${streams.length} detected`;
      const list = document.getElementById("browser-stream-list");
      if (!streams.length) {
        list.innerHTML = '<div class="empty">No streams detected yet. Start playback in the remote browser.</div>';
        return;
      }
      list.innerHTML = streams.map((stream) => streamCard(stream, state.metadata || {}, state)).join("");
      list.querySelectorAll(".queue-btn").forEach((button) => {
        button.addEventListener("click", async () => {
          const url = decodeURIComponent(button.dataset.streamUrl);
          const currentState = window.__browserState || state;
          const metadata = currentState.metadata || {};
          const title = (metadata.raw_title || state.page_title || "Untitled").replace(/\\s*[-|]\\s*Y?Flix.*$/i, "").trim();
          button.disabled = true;
          button.textContent = "Queueing...";
          try {
            const queueResponse = await fetch("/api/browser/queue", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                title,
                url,
                metadata: {
                  ...metadata,
                  page_title: currentState.page_title || "",
                  page_url: currentState.page_url || ""
                }
              })
            });
            if (!queueResponse.ok) {
              throw new Error(`Queue request failed with ${queueResponse.status}`);
            }
            const nextState = {
              ...currentState,
              streams: (currentState.streams || []).filter((stream) => stream.url !== url)
            };
            window.__browserState = nextState;
            await persistBrowserState(nextState);
            refresh();
          } catch (error) {
            button.disabled = false;
            button.textContent = "Retry";
            console.error(error);
          }
        });
      });
    }

    async function refresh() {
      const response = await fetch("/api/tasks");
      window.__taskState = await response.json();
      renderTasks();
      await refreshBrowserState();
    }

    refresh();
    setInterval(refresh, 1500);
  </script>
</body>
</html>
"""
