from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .download_manager import DownloadManager
from .music_manager import MusicManager
from .music_web import install_music
from .youtube_manager import YouTubeManager


EXTENSION_DIR = Path(__file__).resolve().parent.parent / "browser_extension"
LOGGER = logging.getLogger("isambard.web")
BROWSER_SITES = {
    "yflix": "https://yflix.to/",
    "dashflix": "https://dashflix.top/",
}


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


class BrowserNavigateRequest(BaseModel):
    site: str


class YouTubeLookupRequest(BaseModel):
    url: str
    refresh: bool = False


class YouTubeQueueRequest(BaseModel):
    cache_key: str
    video_ids: list[str]


class YouTubeSubscribeRequest(BaseModel):
    cache_key: str


def build_app(
    download_manager: DownloadManager,
    youtube_manager: YouTubeManager,
    music_manager: MusicManager,
) -> FastAPI:
    app = FastAPI(title="Downloader")
    app.mount("/extension", StaticFiles(directory=EXTENSION_DIR), name="extension")
    install_music(app, music_manager)
    app.state.browser_state = {
        "page_url": "",
        "page_title": "",
        "metadata": {},
        "streams": [],
        "can_go_back": False,
        "can_go_forward": False,
        "site": "yflix",
    }
    app.state.browser_command = {"id": 0, "action": "", "value": ""}

    def render_page(active_page: str) -> str:
        browser_port = os.environ.get("ISAMBARD_BROWSER_PORT", "8766")
        browser_url = _build_browser_embed_url(
            os.environ.get("BROWSER_URL", f"http://localhost:{browser_port}"),
            os.environ.get("BROWSER_USER", "guac"),
            os.environ.get("BROWSER_PASSWORD", "guac"),
        )
        browser_user = os.environ.get("BROWSER_USER", "guac")
        browser_password = os.environ.get("BROWSER_PASSWORD", "guac")
        return (
            INDEX_HTML.replace("__BROWSER_URL__", browser_url)
            .replace("__BROWSER_USER__", browser_user)
            .replace("__BROWSER_PASSWORD__", browser_password)
            .replace("__BROWSER_SITE_OPTIONS__", _browser_site_options("yflix"))
            .replace("__DOWNLOADS_TAB_SELECTED__", "true" if active_page == "downloads" else "false")
            .replace("__MOVIES_TAB_SELECTED__", "true" if active_page == "library" else "false")
            .replace("__YOUTUBE_TAB_SELECTED__", "true" if active_page == "youtube" else "false")
            .replace("__MUSIC_TAB_SELECTED__", "true" if active_page == "music" else "false")
            .replace("__SETTINGS_TAB_SELECTED__", "true" if active_page == "settings" else "false")
            .replace("__DOWNLOADS_PAGE_CLASS__", "page is-active" if active_page == "downloads" else "page")
            .replace("__LIBRARY_PAGE_CLASS__", "page is-active" if active_page == "library" else "page")
            .replace("__YOUTUBE_PAGE_CLASS__", "page is-active" if active_page == "youtube" else "page")
            .replace("__MUSIC_PAGE_CLASS__", "page is-active" if active_page == "music" else "page")
            .replace("__SETTINGS_PAGE_CLASS__", "page is-active" if active_page == "settings" else "page")
        )

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/downloads", status_code=307)

    @app.get("/downloads", response_class=HTMLResponse)
    def downloads_page() -> str:
        return render_page("downloads")

    @app.get("/movies-tv", response_class=HTMLResponse)
    def movies_tv_page() -> str:
        return render_page("library")

    @app.get("/youtube", response_class=HTMLResponse)
    def youtube_page() -> str:
        return render_page("youtube")

    @app.get("/music", response_class=HTMLResponse)
    def music_page() -> str:
        return render_page("music")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page() -> str:
        return render_page("settings")

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
            "site": _site_from_url(request.page_url),
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
        app.state.browser_command = {"id": current_id, "action": action, "value": ""}
        LOGGER.info("issued browser command id=%s action=%s", current_id, action)
        return {"ok": True, "id": current_id, "action": action}

    @app.post("/api/browser/navigate")
    def navigate_browser(request: BrowserNavigateRequest) -> dict:
        target = BROWSER_SITES.get(request.site)
        if not target:
            LOGGER.warning("unsupported browser site site=%s", request.site)
            return {"ok": False, "error": "unsupported site"}
        current_id = int(app.state.browser_command.get("id", 0)) + 1
        app.state.browser_command = {"id": current_id, "action": "navigate", "value": target}
        LOGGER.info("issued browser navigate id=%s site=%s target=%s", current_id, request.site, target)
        return {"ok": True, "id": current_id, "action": "navigate", "value": target}

    @app.post("/api/browser/command/ack")
    def acknowledge_browser_command(request: BrowserCommandRequest) -> dict:
        if int(app.state.browser_command.get("id", 0)) == request.command_id:
            app.state.browser_command = {"id": request.command_id, "action": "", "value": ""}
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

    @app.get("/api/youtube/state")
    def youtube_state() -> dict:
        return youtube_manager.state()

    @app.get("/api/settings/general")
    def general_settings_status() -> dict:
        return {"mullvad": _mullvad_status()}

    @app.post("/api/youtube/lookup")
    def youtube_lookup(request: YouTubeLookupRequest) -> dict:
        LOGGER.info("youtube lookup url=%s refresh=%s", request.url, request.refresh)
        lookup = youtube_manager.lookup(request.url, request.refresh)
        return lookup.to_dict()

    @app.post("/api/youtube/queue")
    def youtube_queue(request: YouTubeQueueRequest) -> dict:
        LOGGER.info("youtube queue cache_key=%s count=%s", request.cache_key, len(request.video_ids))
        queued = youtube_manager.queue_selected(request.cache_key, request.video_ids)
        return {"ok": True, "queued": queued}

    @app.post("/api/youtube/subscribe")
    def youtube_subscribe(request: YouTubeSubscribeRequest) -> dict:
        subscription = youtube_manager.subscribe(request.cache_key)
        LOGGER.info("youtube subscribed cache_key=%s subscription_id=%s", request.cache_key, subscription["id"])
        return {"ok": True, "subscription": subscription}

    @app.post("/api/youtube/subscriptions/{subscription_id}/refresh")
    def refresh_youtube_subscription(subscription_id: str) -> dict:
        LOGGER.info("youtube subscription refresh id=%s", subscription_id)
        return youtube_manager.refresh_subscription(subscription_id)

    @app.delete("/api/youtube/subscriptions/{subscription_id}")
    def delete_youtube_subscription(subscription_id: str) -> dict:
        removed = youtube_manager.remove_subscription(subscription_id)
        LOGGER.info("youtube subscription delete id=%s removed=%s", subscription_id, removed)
        return {"ok": removed}

    return app


def _site_from_url(url: str) -> str:
    lowered = (url or "").lower()
    if lowered.startswith("https://dashflix.top/"):
        return "dashflix"
    return "yflix"


def _browser_site_options(selected_site: str) -> str:
    labels = {
        "yflix": "YFlix",
        "dashflix": "DashFlix",
    }
    parts = []
    for key, label in labels.items():
        selected = " selected" if key == selected_site else ""
        parts.append(f'<option value="{key}"{selected}>{label}</option>')
    return "".join(parts)


def _build_browser_embed_url(base_url: str, username: str, password: str) -> str:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("username", username)
    query.setdefault("password", password)
    query.setdefault("preventSleep", "true")
    query.setdefault("resize", "display-update")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _mullvad_status() -> dict:
    try:
        with urllib.request.urlopen("https://am.i.mullvad.net/json", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOGGER.warning("failed to read mullvad endpoint: %s", exc)
        if shutil.which("mullvad") is None:
            return {
                "available": False,
                "connected": False,
                "summary": "Unable to reach Mullvad status endpoint",
            }
        try:
            result = subprocess.run(
                ["mullvad", "status"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception as cli_exc:
            LOGGER.warning("failed to read mullvad status: %s", cli_exc)
            return {
                "available": False,
                "connected": False,
                "summary": "Unable to read Mullvad status",
            }
        output = (result.stdout or result.stderr or "").strip()
        lowered = output.lower()
        connected = "connected" in lowered and "disconnected" not in lowered
        if not output:
            output = "Mullvad status unavailable"
        return {
            "available": True,
            "connected": connected,
            "summary": output,
        }
    connected = bool(payload.get("mullvad_exit_ip"))
    ip = payload.get("ip") or "Unknown IP"
    location = ", ".join(
        part for part in [payload.get("city"), payload.get("country")] if isinstance(part, str) and part
    )
    organization = payload.get("organization") or ""
    summary_parts = [f"IP: {ip}"]
    if location:
        summary_parts.append(f"Location: {location}")
    if organization:
        summary_parts.append(f"Org: {organization}")
    return {
        "available": True,
        "connected": connected,
        "summary": "\n".join(summary_parts),
    }


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
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      backdrop-filter: blur(18px);
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.22);
    }
    .title {
      margin: 0;
      font-size: clamp(24px, 3vw, 34px);
      font-weight: 800;
      letter-spacing: -0.04em;
    }
    .header-main {
      display: flex;
      align-items: center;
      gap: 18px;
      min-width: 0;
    }
    .tabs {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .tab-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04);
      color: var(--muted);
      border-radius: 12px;
      padding: 10px 14px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
    }
    .tab-btn:hover {
      background: rgba(255,255,255,0.08);
      color: var(--text);
    }
    .tab-btn[aria-selected="true"] {
      color: var(--text);
      background: rgba(15,123,255,0.16);
      border-color: rgba(15,123,255,0.32);
    }
    .page {
      display: none;
    }
    .page.is-active {
      display: block;
    }
    .youtube-shell {
      display: grid;
      gap: 16px;
    }
    .youtube-lookup-form {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto auto;
      gap: 12px;
      align-items: center;
    }
    .youtube-input {
      width: 100%;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 14px;
      background: rgba(255,255,255,0.04);
      color: var(--text);
      padding: 12px 14px;
      font-size: 14px;
      min-width: 0;
    }
    .youtube-btn {
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 14px;
      background: rgba(255,255,255,0.06);
      color: var(--text);
      padding: 12px 16px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
    }
    .youtube-btn.primary {
      background: linear-gradient(90deg, rgba(15,123,255,0.95), rgba(57,183,255,0.95));
      border-color: rgba(57,183,255,0.45);
    }
    .youtube-btn:disabled {
      opacity: 0.6;
      cursor: default;
    }
    .youtube-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(300px, 0.38fr);
      gap: 16px;
      align-items: start;
    }
    .youtube-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      height: 520px;
      min-height: 520px;
      max-height: 520px;
      overflow-y: scroll;
      overflow-x: hidden;
      scrollbar-gutter: stable;
      padding: 14px;
    }
    .youtube-item {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 18px;
      padding: 12px 14px;
      background: rgba(255,255,255,0.03);
    }
    .youtube-item-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      margin-top: 6px;
    }
    .youtube-item-title {
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
      min-width: 0;
    }
    .youtube-check {
      margin-top: 2px;
      width: 18px;
      height: 18px;
    }
    .youtube-status {
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
    }
    .youtube-status.downloaded { color: var(--completed); }
    .youtube-status.running { color: var(--running); }
    .youtube-status.queued { color: var(--queued); }
    .youtube-status.failed { color: var(--failed); }
    .youtube-status.stopped { color: var(--queued); }
    .youtube-side-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      height: 520px;
      min-height: 520px;
      max-height: 520px;
      overflow-y: scroll;
      overflow-x: hidden;
      scrollbar-gutter: stable;
      padding: 14px;
    }
    .youtube-card {
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 18px;
      background: rgba(255,255,255,0.03);
      padding: 14px;
      display: grid;
      gap: 10px;
    }
    .youtube-small {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      word-break: break-word;
    }
    .youtube-toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
    }
    .youtube-toolbar-left,
    .youtube-toolbar-right {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }
    .music-shell {
      display: grid;
      gap: 16px;
    }
    .music-hero {
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(280px, 0.75fr);
      gap: 16px;
    }
    .music-hero-copy {
      display: grid;
      gap: 10px;
      padding: 18px;
    }
    .music-hero-copy p {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }
    .music-eyebrow {
      color: #abc0dc;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .music-hero-metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      padding: 18px;
    }
    .music-metric {
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 18px;
      background: rgba(255,255,255,0.03);
      padding: 14px;
    }
    .music-metric-value {
      display: block;
      font-size: 28px;
      font-weight: 800;
      line-height: 1;
    }
    .music-metric-label {
      display: block;
      margin-top: 8px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .music-grid {
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }
    .music-nav {
      display: grid;
      gap: 10px;
      padding: 14px;
    }
    .music-nav-btn {
      width: 100%;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 14px;
      padding: 11px 13px;
      background: rgba(255,255,255,0.03);
      color: var(--muted);
      text-align: left;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
    }
    .music-nav-btn.is-active {
      background: rgba(15,123,255,0.16);
      border-color: rgba(15,123,255,0.32);
      color: var(--text);
    }
    .music-panel-stack {
      display: grid;
      gap: 16px;
    }
    .music-subpage {
      display: none;
    }
    .music-subpage.is-active {
      display: block;
    }
    .music-form {
      display: grid;
      gap: 14px;
    }
    .music-input {
      width: 100%;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 14px;
      background: rgba(255,255,255,0.04);
      color: var(--text);
      padding: 12px 14px;
      font-size: 14px;
      min-width: 0;
    }
    .music-field {
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    .music-checkbox {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: var(--text);
      font-size: 13px;
      font-weight: 600;
    }
    .music-actions {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .music-btn {
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 14px;
      background: rgba(255,255,255,0.06);
      color: var(--text);
      padding: 11px 15px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
    }
    .music-btn.primary {
      background: linear-gradient(90deg, rgba(15,123,255,0.95), rgba(57,183,255,0.95));
      border-color: rgba(57,183,255,0.45);
    }
    .music-list {
      display: grid;
      gap: 12px;
      padding: 14px;
    }
    .music-list-item {
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 18px;
      background: rgba(255,255,255,0.03);
      padding: 14px;
      display: grid;
      gap: 6px;
    }
    .music-list-item h3,
    .music-list-item h4,
    .music-list-item p {
      margin: 0;
    }
    .music-list-item p {
      color: var(--muted);
      line-height: 1.45;
      word-break: break-word;
    }
    .music-metadata {
      padding: 14px;
      display: grid;
      gap: 14px;
    }
    .music-metadata-header {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 14px;
      align-items: center;
    }
    .music-metadata-image {
      width: 96px;
      height: 96px;
      border-radius: 20px;
      object-fit: cover;
      background: rgba(255,255,255,0.04);
    }
    .music-track-list {
      display: grid;
      gap: 8px;
    }
    .music-track-row {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      border-radius: 14px;
      background: rgba(255,255,255,0.04);
      padding: 10px 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .settings-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }
    .settings-card {
      padding: 18px;
      display: grid;
      gap: 14px;
      align-content: start;
      min-height: 220px;
    }
    .settings-card.empty-card {
      color: var(--muted);
    }
    .settings-status {
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--text);
      font-size: 14px;
      font-weight: 700;
    }
    .settings-status-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--queued);
      box-shadow: 0 0 0 6px rgba(139, 149, 167, 0.12);
      flex: 0 0 auto;
    }
    .settings-status-dot.connected {
      background: var(--completed);
      box-shadow: 0 0 0 6px rgba(34, 197, 94, 0.14);
    }
    .settings-status-dot.disconnected {
      background: var(--failed);
      box-shadow: 0 0 0 6px rgba(239, 68, 68, 0.14);
    }
    .settings-summary {
      color: var(--muted);
      line-height: 1.5;
      font-size: 13px;
      word-break: break-word;
      white-space: pre-wrap;
    }
    .top-grid {
      display: grid;
      grid-template-columns: minmax(0, 2.15fr) minmax(240px, 0.45fr);
      gap: 16px;
      align-items: stretch;
      margin-bottom: 16px;
    }
    .top-grid > * {
      min-height: 0;
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
      overflow: hidden;
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
    .browser-site-select {
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px;
      padding: 10px 12px;
      background: rgba(255,255,255,0.06);
      color: var(--text);
      font-size: 13px;
      font-weight: 700;
      min-width: 138px;
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
      overscroll-behavior: contain;
      touch-action: pan-x pan-y pinch-zoom;
    }
    .browser-frame:focus {
      outline: 2px solid rgba(15,123,255,0.9);
      outline-offset: -2px;
    }
    .stream-panel {
      align-self: stretch;
      height: 100%;
      max-height: 100%;
      min-height: 0;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      overflow: hidden;
    }
    .stream-list {
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      flex: 1 1 auto;
      height: 100%;
      max-height: 100%;
      min-height: 0;
      overflow-y: scroll;
      overflow-x: hidden;
      scrollbar-gutter: stable;
      align-items: stretch;
    }
    .stream-item {
      flex: 0 0 auto;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 18px;
      padding: 14px;
      min-width: 0;
      width: 100%;
      overflow: hidden;
    }
    .stream-item.is-queued,
    .stream-item.is-running,
    .stream-item.is-completed {
      opacity: 0.56;
      background: rgba(255,255,255,0.02);
      border-color: rgba(255,255,255,0.04);
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
      display: flex;
      flex-direction: column;
      gap: 12px;
      flex: 1 1 auto;
      height: 100%;
      overflow-y: scroll;
      overflow-x: hidden;
      scrollbar-gutter: stable;
      min-height: 0;
      align-items: stretch;
    }
    .task-list-wrap {
      display: none;
      min-height: 0;
      overflow: hidden;
    }
    .task-panel[open] .task-list-wrap {
      height: 420px;
      min-height: 420px;
      max-height: 420px;
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
      flex: 0 0 auto;
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
    .task-kind {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.06);
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      line-height: 1;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      white-space: nowrap;
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
      .header {
        flex-direction: column;
        align-items: stretch;
      }
      .header-main {
        justify-content: space-between;
      }
      .top-grid {
        grid-template-columns: 1fr;
      }
      .youtube-grid {
        grid-template-columns: 1fr;
      }
      .music-hero,
      .music-grid {
        grid-template-columns: 1fr;
      }
      .music-nav {
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
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
      .browser-body {
        aspect-ratio: auto;
        min-height: 84vh;
      }
      .browser-controls {
        gap: 8px;
      }
      .browser-site-select {
        min-width: 0;
        flex: 1 1 auto;
      }
      .browser-btn {
        min-width: 48px;
        height: 44px;
      }
      .task-head {
        grid-template-columns: 1fr;
      }
      .stream-actions {
        align-items: start;
        flex-direction: column;
      }
      .youtube-lookup-form {
        grid-template-columns: 1fr;
      }
      .music-hero-metrics {
        grid-template-columns: 1fr;
      }
      .music-actions {
        flex-direction: column;
        align-items: stretch;
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
      <div class="header-main">
        <h1 class="title">Isambard</h1>
      </div>
      <nav class="tabs" aria-label="Primary">
        <a class="tab-btn" href="/downloads" aria-selected="__DOWNLOADS_TAB_SELECTED__">Downloads</a>
        <a class="tab-btn" href="/movies-tv" aria-selected="__MOVIES_TAB_SELECTED__">Movies &amp; TV</a>
        <a class="tab-btn" href="/youtube" aria-selected="__YOUTUBE_TAB_SELECTED__">YouTube</a>
        <a class="tab-btn" href="/music" aria-selected="__MUSIC_TAB_SELECTED__">Music</a>
        <a class="tab-btn" href="/settings" aria-selected="__SETTINGS_TAB_SELECTED__">Settings</a>
      </nav>
    </section>

    <section class="__DOWNLOADS_PAGE_CLASS__" data-page="downloads">
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
    </section>

    <section class="__LIBRARY_PAGE_CLASS__" data-page="library">
      <section class="top-grid">
        <section class="browser-wrap">
          <div class="panel browser-shell">
          <div class="panel-header">
            <div class="panel-title-row">
                <select class="browser-site-select" id="browser-site-select" aria-label="Browser site">
                  __BROWSER_SITE_OPTIONS__
                </select>
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
    </section>

    <section class="__YOUTUBE_PAGE_CLASS__" data-page="youtube">
      <section class="youtube-shell">
        <section class="panel">
          <header class="panel-header">
            <h2 class="panel-title">Lookup</h2>
            <span class="pill" id="youtube-lookup-pill">Cached lookups</span>
          </header>
          <div style="padding: 14px;">
            <form class="youtube-lookup-form" id="youtube-lookup-form">
              <input id="youtube-url" class="youtube-input" type="url" placeholder="Paste YouTube video, playlist, or channel URL" required>
              <button class="youtube-btn primary" type="submit" id="youtube-lookup-btn">Lookup</button>
              <button class="youtube-btn" type="button" id="youtube-refresh-btn">Refresh</button>
              <button class="youtube-btn" type="button" id="youtube-subscribe-btn">Subscribe</button>
            </form>
          </div>
        </section>

        <section class="youtube-grid">
          <section class="panel">
            <header class="panel-header">
              <div class="panel-title-row">
                <h2 class="panel-title">Videos</h2>
              </div>
              <span class="pill" id="youtube-selected-pill">0 selected</span>
            </header>
            <div style="padding: 14px 14px 0;">
              <div class="youtube-toolbar">
                <div class="youtube-toolbar-left">
                  <label class="youtube-small"><input type="checkbox" id="youtube-select-all"> Select all visible</label>
                </div>
                <div class="youtube-toolbar-right">
                  <button class="youtube-btn primary" type="button" id="youtube-queue-btn">Queue selected</button>
                </div>
              </div>
            </div>
            <div class="youtube-list" id="youtube-results-list"></div>
          </section>

          <section class="panel">
            <header class="panel-header">
              <h2 class="panel-title">Subscriptions</h2>
              <span class="pill" id="youtube-subscriptions-pill">0 active</span>
            </header>
            <div class="youtube-side-list" id="youtube-subscriptions-list"></div>
          </section>
        </section>
      </section>
    </section>

    <section class="__MUSIC_PAGE_CLASS__" data-page="music">
      <section class="music-shell">
        <section class="music-hero">
          <section class="panel music-hero-copy">
            <span class="music-eyebrow">Music</span>
            <h2 class="panel-title">SpotiFLAC Python Rewrite</h2>
            <p>Spotify metadata fetch, queue state, history, and settings are available here through the Python implementation, but rendered with the same Isambard shell and theme as the rest of the app.</p>
          </section>
          <section class="panel music-hero-metrics">
            <div class="music-metric">
              <span class="music-metric-value" id="music-metric-fetches">0</span>
              <span class="music-metric-label">Fetches</span>
            </div>
            <div class="music-metric">
              <span class="music-metric-value" id="music-metric-downloads">0</span>
              <span class="music-metric-label">Downloads</span>
            </div>
            <div class="music-metric">
              <span class="music-metric-value" id="music-metric-queue">0</span>
              <span class="music-metric-label">Queued</span>
            </div>
          </section>
        </section>

        <section class="music-grid">
          <aside class="panel music-nav">
            <button class="music-nav-btn is-active" data-music-panel="fetch" type="button">Fetch</button>
            <button class="music-nav-btn" data-music-panel="queue" type="button">Queue</button>
            <button class="music-nav-btn" data-music-panel="history" type="button">History</button>
            <button class="music-nav-btn" data-music-panel="settings" type="button">Settings</button>
            <button class="music-nav-btn" data-music-panel="tools" type="button">Tools</button>
          </aside>

          <section class="music-panel-stack">
            <section class="music-subpage is-active" id="music-panel-fetch">
              <section class="panel" style="padding: 14px;">
                <header class="panel-header">
                  <div class="panel-title-row">
                    <h2 class="panel-title">Fetch Spotify Metadata</h2>
                  </div>
                </header>
                <form class="music-form" id="music-fetch-form">
                  <input id="music-spotify-url" class="music-input" type="text" placeholder="https://open.spotify.com/album/..." />
                  <div class="music-actions">
                    <button class="music-btn primary" type="submit">Fetch</button>
                  </div>
                </form>
              </section>
              <section class="panel music-metadata">
                <header class="panel-header">
                  <div class="panel-title-row">
                    <h2 class="panel-title">Current Result</h2>
                  </div>
                </header>
                <div id="music-metadata-result" class="empty">No metadata fetched yet.</div>
              </section>
            </section>

            <section class="music-subpage" id="music-panel-queue">
              <section class="panel">
                <header class="panel-header">
                  <div class="panel-title-row">
                    <h2 class="panel-title">Download Queue</h2>
                  </div>
                  <div class="music-actions">
                    <button class="music-btn" type="button" id="music-refresh-queue">Refresh</button>
                    <button class="music-btn" type="button" id="music-clear-queue">Clear Queue</button>
                  </div>
                </header>
                <div class="music-list" id="music-queue-list"></div>
              </section>
            </section>

            <section class="music-subpage" id="music-panel-history">
              <section class="youtube-grid">
                <section class="panel">
                  <header class="panel-header">
                    <div class="panel-title-row">
                      <h2 class="panel-title">Fetch History</h2>
                    </div>
                    <div class="music-actions">
                      <button class="music-btn" type="button" id="music-refresh-fetch-history">Refresh</button>
                      <button class="music-btn" type="button" id="music-clear-fetch-history">Clear</button>
                    </div>
                  </header>
                  <div class="music-list" id="music-fetch-history-list"></div>
                </section>
                <section class="panel">
                  <header class="panel-header">
                    <div class="panel-title-row">
                      <h2 class="panel-title">Download History</h2>
                    </div>
                    <div class="music-actions">
                      <button class="music-btn" type="button" id="music-refresh-download-history">Refresh</button>
                      <button class="music-btn" type="button" id="music-clear-download-history">Clear</button>
                    </div>
                  </header>
                  <div class="music-list" id="music-download-history-list"></div>
                </section>
              </section>
            </section>

            <section class="music-subpage" id="music-panel-settings">
              <section class="panel" style="padding: 14px;">
                <header class="panel-header">
                  <div class="panel-title-row">
                    <h2 class="panel-title">Settings</h2>
                  </div>
                </header>
                <form class="music-form" id="music-settings-form">
                  <label class="music-field">
                    <span>SpotFetch API URL</span>
                    <input id="music-spotfetch-api-url" class="music-input" type="text" />
                  </label>
                  <label class="music-checkbox">
                    <input id="music-use-spotfetch-api" type="checkbox" />
                    <span>Use SpotFetch API</span>
                  </label>
                  <label class="music-field">
                    <span>Download Path</span>
                    <input id="music-download-path" class="music-input" type="text" placeholder="/Users/you/Music" />
                  </label>
                  <div class="music-actions">
                    <button class="music-btn primary" type="submit">Save Settings</button>
                  </div>
                </form>
              </section>
            </section>

            <section class="music-subpage" id="music-panel-tools">
              <section class="panel" style="padding: 14px;">
                <header class="panel-header">
                  <div class="panel-title-row">
                    <h2 class="panel-title">Tools</h2>
                  </div>
                </header>
                <div class="music-actions">
                  <span class="pill">Audio Analysis</span>
                  <span class="pill">Audio Converter</span>
                  <span class="pill">Audio Resampler</span>
                  <span class="pill">File Manager</span>
                  <span class="pill">Lyrics</span>
                  <span class="pill">Cover Downloads</span>
                  <span class="pill">Availability Checks</span>
                  <span class="pill">FFmpeg Management</span>
                </div>
              </section>
            </section>
          </section>
        </section>
      </section>
    </section>

    <section class="__SETTINGS_PAGE_CLASS__" data-page="settings">
      <section class="settings-grid">
        <section class="panel settings-card">
          <header class="panel-header">
            <div class="panel-title-row">
              <h2 class="panel-title">General</h2>
            </div>
          </header>
          <div class="settings-status">
            <span class="settings-status-dot" id="mullvad-status-dot" aria-hidden="true"></span>
            <span id="mullvad-status-label">Checking Mullvad status...</span>
          </div>
          <div class="settings-summary" id="mullvad-status-summary"></div>
        </section>
        <section class="panel settings-card empty-card">
          <header class="panel-header">
            <div class="panel-title-row">
              <h2 class="panel-title">Downloads</h2>
            </div>
          </header>
        </section>
        <section class="panel settings-card empty-card">
          <header class="panel-header">
            <div class="panel-title-row">
              <h2 class="panel-title">Movies &amp; TV</h2>
            </div>
          </header>
        </section>
        <section class="panel settings-card empty-card">
          <header class="panel-header">
            <div class="panel-title-row">
              <h2 class="panel-title">YouTube</h2>
            </div>
          </header>
        </section>
        <section class="panel settings-card empty-card">
          <header class="panel-header">
            <div class="panel-title-row">
              <h2 class="panel-title">Music</h2>
            </div>
          </header>
        </section>
      </section>
    </section>
  </main>

  <script>
    const browserFrame = document.getElementById("browser-frame");
    const browserControls = document.querySelector(".browser-controls");
    const browserSiteSelect = document.getElementById("browser-site-select");
    const isTouchDevice = window.matchMedia("(pointer: coarse)").matches || navigator.maxTouchPoints > 0;
    let browserHasIntentFocus = false;
    let browserFocusTimer = null;
    let browserControlLock = false;
    let pendingBrowserSite = "";
    let lastBrowserPageUrl = "";
    let lastBrowserSite = "";
    function cancelBrowserRefocus() {
      if (browserFocusTimer) {
        clearTimeout(browserFocusTimer);
        browserFocusTimer = null;
      }
    }
    function focusBrowserFrame() {
      if (!browserFrame || browserControlLock || isTouchDevice) {
        return;
      }
      browserHasIntentFocus = true;
      browserFrame.focus();
    }
    function scheduleBrowserRefocus() {
      if (browserControlLock) {
        return;
      }
      cancelBrowserRefocus();
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
    function lockBrowserControls() {
      browserControlLock = true;
      cancelBrowserRefocus();
      browserHasIntentFocus = false;
    }
    function unlockBrowserControlsSoon() {
      setTimeout(() => {
        browserControlLock = false;
      }, 150);
    }
    browserControls?.addEventListener("mousedown", lockBrowserControls, true);
    browserControls?.addEventListener("click", lockBrowserControls, true);
    browserControls?.addEventListener("focusin", lockBrowserControls, true);
    browserControls?.addEventListener("focusout", () => {
      unlockBrowserControlsSoon();
    }, true);
    browserSiteSelect?.addEventListener("mousedown", (event) => {
      lockBrowserControls();
      event.stopPropagation();
    }, true);
    browserSiteSelect?.addEventListener("click", (event) => {
      lockBrowserControls();
      event.stopPropagation();
    }, true);
    document.querySelector(".browser-body")?.addEventListener("mousedown", () => {
      browserControlLock = false;
      scheduleBrowserRefocus();
    });
    document.querySelector(".browser-body")?.addEventListener("touchstart", () => {
      browserControlLock = false;
      browserHasIntentFocus = true;
    }, { passive: true });
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
            const streamHeader = streamPanel.querySelector(".panel-header");
            const streamList = streamPanel.querySelector(".stream-list");
            if (streamHeader && streamList) {
              const headerHeight = Math.round(streamHeader.getBoundingClientRect().height);
              const listHeight = Math.max(0, wrapHeight - headerHeight);
              streamList.style.height = `${listHeight}px`;
              streamList.style.maxHeight = `${listHeight}px`;
            }
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
    document.getElementById("browser-site-select")?.addEventListener("change", async (event) => {
      const select = event.currentTarget;
      const site = select.value;
      if (!site) {
        return;
      }
      pendingBrowserSite = site;
      select.disabled = true;
      try {
        await fetch("/api/browser/navigate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ site })
        });
        scheduleBrowserRefocus();
      } finally {
        setTimeout(() => {
          select.disabled = false;
        }, 400);
      }
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
      if (browserHasIntentFocus && !isTouchDevice) {
        scheduleBrowserRefocus();
      }
    });

    function updateBrowserButtons(state) {
      const backButton = document.querySelector('[data-browser-action="back"]');
      const forwardButton = document.querySelector('[data-browser-action="forward"]');
      const siteSelect = document.getElementById("browser-site-select");
      const pageChanged = !!state.page_url && state.page_url !== lastBrowserPageUrl;
      const siteChanged = !!state.site && state.site !== lastBrowserSite;
      if (backButton) {
        backButton.disabled = !state.can_go_back;
      }
      if (forwardButton) {
        forwardButton.disabled = !state.can_go_forward;
      }
      if (siteSelect) {
        const effectiveSite = pendingBrowserSite || state.site || "yflix";
        siteSelect.value = effectiveSite;
        if (pendingBrowserSite && state.site === pendingBrowserSite) {
          pendingBrowserSite = "";
          browserControlLock = false;
          browserHasIntentFocus = true;
          if (!isTouchDevice) {
            setTimeout(() => {
              scheduleBrowserRefocus();
            }, 120);
          }
        }
      }
      if ((pageChanged || siteChanged) && !browserControlLock && !isTouchDevice) {
        browserHasIntentFocus = true;
        setTimeout(() => {
          scheduleBrowserRefocus();
        }, 120);
      }
      lastBrowserPageUrl = state.page_url || lastBrowserPageUrl;
      lastBrowserSite = state.site || lastBrowserSite;
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

    function cleanMediaTitle(value) {
      return String(value || "")
        .replace(/\\s*[-|]\\s*Y?Flix.*$/i, "")
        .replace(/\\s*-\\s*Watch Now on Dashflix\\s*$/i, "")
        .replace(/\\s*[-|]\\s*DashFlix.*$/i, "")
        .trim();
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
      const stateClass = task ? ` is-${escapeHtml(task.status)}` : "";
      const title = cleanMediaTitle(metadata?.raw_title || state.page_title || "Detected stream") || "Detected stream";
      const metaLine = metadata?.season && metadata?.episode
        ? `Season ${metadata.season} • Episode ${metadata.episode}`
        : "Queue with yt-dlp";
      return `
        <article class="stream-item${stateClass}">
          <div class="stream-title">${escapeHtml(title)}</div>
          <div class="stream-meta">${escapeHtml(metaLine)}</div>
          <div class="stream-url">${escapeHtml(stream.url)}</div>
          <div class="stream-actions">
            <div class="stream-meta">${escapeHtml(task ? queueButtonLabel(task) : (state.page_url || ""))}</div>
            <button class="queue-btn" data-stream-url="${encodeURIComponent(stream.url)}" ${disabled} aria-label="Add to queue" title="Add to queue">${escapeHtml(task ? queueButtonLabel(task) : "+")}</button>
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
      const sourceLabel = taskSourceLabel(task);
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
                  <div class="task-kind">${escapeHtml(sourceLabel)}</div>
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

    function taskSourceLabel(task) {
      const source = String(task.source_type || task.media_type || "standard").toLowerCase();
      if (source === "tv") return "TV";
      if (source === "movie") return "Movie";
      if (source === "youtube") return "YouTube";
      return source || "Standard";
    }

    function youtubeStatusLabel(status) {
      if (!status) return "";
      if (status === "downloaded") return "Downloaded";
      if (status === "queued") return "Queued";
      if (status === "running") return "Downloading";
      if (status === "failed") return "Failed";
      if (status === "stopped") return "Stopped";
      return status;
    }

    function renderYouTubeResults() {
      const list = document.getElementById("youtube-results-list");
      const selectedPill = document.getElementById("youtube-selected-pill");
      const lookupPill = document.getElementById("youtube-lookup-pill");
      const lookup = window.__youtubeState?.latest_lookup;
      const selected = window.__youtubeSelected || new Set();
      if (!lookup) {
        list.innerHTML = '<div class="empty">Look up a YouTube video, playlist, or channel to inspect its videos.</div>';
        selectedPill.textContent = "0 selected";
        lookupPill.textContent = "Cached lookups";
        return;
      }
      lookupPill.textContent = `${lookup.source_title || "Lookup"} • ${lookup.entries.length} videos`;
      if (!lookup.entries.length) {
        list.innerHTML = '<div class="empty">No videos found for this source.</div>';
        selectedPill.textContent = "0 selected";
        return;
      }
      list.innerHTML = lookup.entries.map((entry) => {
        const checked = selected.has(entry.id) ? "checked" : "";
        const disabled = entry.status === "downloaded" ? "disabled" : "";
        const status = entry.status ? `<span class="youtube-status ${escapeHtml(entry.status)}">${escapeHtml(youtubeStatusLabel(entry.status))}</span>` : "";
        const meta = [
          entry.channel_id || "",
          entry.upload_date || "",
          entry.url || "",
        ].filter(Boolean);
        return `
          <label class="youtube-item">
            <input class="youtube-check" type="checkbox" data-youtube-id="${escapeHtml(entry.id)}" ${checked} ${disabled}>
            <div>
              <div class="youtube-item-title">${escapeHtml(entry.title)}</div>
              <div class="youtube-item-meta">${meta.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
            </div>
            ${status}
          </label>
        `;
      }).join("");
      list.querySelectorAll(".youtube-check[data-youtube-id]").forEach((checkbox) => {
        checkbox.addEventListener("change", () => {
          const id = checkbox.dataset.youtubeId;
          if (!id) return;
          if (checkbox.checked) {
            selected.add(id);
          } else {
            selected.delete(id);
          }
          updateYouTubeSelectionUI();
        });
      });
      updateYouTubeSelectionUI();
    }

    function renderYouTubeSubscriptions() {
      const list = document.getElementById("youtube-subscriptions-list");
      const subscriptions = window.__youtubeState?.subscriptions || [];
      document.getElementById("youtube-subscriptions-pill").textContent = `${subscriptions.length} active`;
      if (!subscriptions.length) {
        list.innerHTML = '<div class="empty">No subscriptions yet.</div>';
        return;
      }
      list.innerHTML = subscriptions.map((subscription) => `
        <article class="youtube-card">
          <div class="youtube-item-title">${escapeHtml(subscription.source_title || subscription.source_url)}</div>
          <div class="youtube-small">${escapeHtml(subscription.source_url)}</div>
          <div class="youtube-small">Known videos: ${escapeHtml(String((subscription.known_video_ids || []).length))}</div>
          <div class="youtube-small">Last checked: ${escapeHtml(subscription.last_checked_at || "Never")}</div>
          <div class="youtube-toolbar">
            <div class="youtube-toolbar-left"></div>
            <div class="youtube-toolbar-right">
              <button class="youtube-btn" type="button" data-youtube-refresh-sub="${escapeHtml(subscription.id)}">Refresh</button>
              <button class="youtube-btn" type="button" data-youtube-remove-sub="${escapeHtml(subscription.id)}">Remove</button>
            </div>
          </div>
        </article>
      `).join("");
      list.querySelectorAll("[data-youtube-refresh-sub]").forEach((button) => {
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await fetch(`/api/youtube/subscriptions/${button.dataset.youtubeRefreshSub}/refresh`, { method: "POST" });
            await refreshYouTubeState();
            await refresh();
          } finally {
            button.disabled = false;
          }
        });
      });
      list.querySelectorAll("[data-youtube-remove-sub]").forEach((button) => {
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await fetch(`/api/youtube/subscriptions/${button.dataset.youtubeRemoveSub}`, { method: "DELETE" });
            await refreshYouTubeState();
          } finally {
            button.disabled = false;
          }
        });
      });
    }

    function updateYouTubeSelectionUI() {
      const lookup = window.__youtubeState?.latest_lookup;
      const selected = window.__youtubeSelected || new Set();
      document.getElementById("youtube-selected-pill").textContent = `${selected.size} selected`;
      const selectableIds = new Set((lookup?.entries || []).filter((entry) => entry.status !== "downloaded").map((entry) => entry.id));
      const selectAll = document.getElementById("youtube-select-all");
      if (selectAll) {
        const totalSelectable = selectableIds.size;
        const selectedCount = Array.from(selected).filter((id) => selectableIds.has(id)).length;
        selectAll.checked = totalSelectable > 0 && selectedCount === totalSelectable;
        selectAll.indeterminate = selectedCount > 0 && selectedCount < totalSelectable;
      }
    }

    function switchMusicPanel(name) {
      document.querySelectorAll(".music-nav-btn[data-music-panel]").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.musicPanel === name);
      });
      document.querySelectorAll(".music-subpage").forEach((panel) => {
        panel.classList.toggle("is-active", panel.id === `music-panel-${name}`);
      });
    }

    function updateMusicMetrics() {
      const fetches = window.__musicFetchHistory || [];
      const downloads = window.__musicDownloadHistory || [];
      const queue = window.__musicQueueSummary?.queue || [];
      const queuedCount = queue.filter((item) => item.status === "queued" || item.status === "downloading").length;
      const fetchNode = document.getElementById("music-metric-fetches");
      const downloadNode = document.getElementById("music-metric-downloads");
      const queueNode = document.getElementById("music-metric-queue");
      if (fetchNode) fetchNode.textContent = String(fetches.length);
      if (downloadNode) downloadNode.textContent = String(downloads.length);
      if (queueNode) queueNode.textContent = String(queuedCount);
    }

    function renderMusicList(listId, items, renderItem, emptyMessage) {
      const list = document.getElementById(listId);
      if (!list) return;
      if (!items.length) {
        list.innerHTML = `<div class="empty">${escapeHtml(emptyMessage)}</div>`;
        return;
      }
      list.innerHTML = items.map(renderItem).join("");
    }

    function renderMusicMetadata() {
      const container = document.getElementById("music-metadata-result");
      const summary = window.__musicMetadata || null;
      if (!container) return;
      if (!summary) {
        container.className = "empty";
        container.textContent = "No metadata fetched yet.";
        return;
      }
      const payload = summary.payload || {};
      const tracks = payload.track_list || [];
      const queueButton = tracks.length ? `<button type="button" class="music-btn primary" id="music-queue-first-track">Queue First Track</button>` : "";
      const rows = tracks.slice(0, 12).map((track) => `
        <div class="music-track-row">
          <span>${escapeHtml(track.name || track.title || "Track")}</span>
          <span>${escapeHtml(track.artists || track.artist || "")}</span>
        </div>
      `).join("");
      container.className = "";
      container.innerHTML = `
        <div class="music-metadata-header">
          ${summary.image ? `<img class="music-metadata-image" src="${escapeHtml(summary.image)}" alt="">` : ""}
          <div>
            <h3>${escapeHtml(summary.title)}</h3>
            <p class="task-extra">${escapeHtml(summary.subtitle || "")}</p>
            <p class="task-extra">${escapeHtml(summary.entity_type)} • ${escapeHtml(String(summary.track_count || 0))}</p>
          </div>
        </div>
        <div class="music-actions">${queueButton}</div>
        <div class="music-track-list">${rows || `<div class="empty">No track list in this payload.</div>`}</div>
      `;
      document.getElementById("music-queue-first-track")?.addEventListener("click", async () => {
        const first = tracks[0];
        if (!first) return;
        await fetch("/api/music/queue", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            spotify_id: first.spotify_id || "",
            track_name: first.name || first.title || summary.title,
            artist_name: first.artists || first.artist || summary.subtitle || "",
            album_name: first.album_name || payload.album_info?.name || ""
          })
        });
        await refreshMusicQueue();
        switchMusicPanel("queue");
      });
    }

    async function refreshMusicQueue() {
      const response = await fetch("/api/music/queue");
      window.__musicQueueSummary = await response.json();
      const items = window.__musicQueueSummary.queue || [];
      renderMusicList("music-queue-list", items, (item) => `
        <article class="music-list-item">
          <h4>${escapeHtml(item.track_name)}</h4>
          <p>${escapeHtml(item.artist_name)}${item.album_name ? ` • ${escapeHtml(item.album_name)}` : ""}</p>
          <p>Status: ${escapeHtml(item.status)} • Progress: ${escapeHtml(String(item.progress))}%</p>
          <div class="music-actions">
            ${item.status === "queued" ? `<button class="music-btn primary" type="button" data-music-start-id="${escapeHtml(item.id)}">Start</button>` : ""}
          </div>
        </article>
      `, "Queue is empty.");
      document.querySelectorAll("[data-music-start-id]").forEach((button) => {
        button.addEventListener("click", async () => {
          button.disabled = true;
          await fetch("/api/music/queue/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ item_id: button.dataset.musicStartId })
          });
          await refreshMusicQueue();
          await refreshMusicDownloadHistory();
        });
      });
      updateMusicMetrics();
    }

    async function refreshMusicFetchHistory() {
      const response = await fetch("/api/music/history/fetch");
      window.__musicFetchHistory = await response.json();
      renderMusicList("music-fetch-history-list", window.__musicFetchHistory, (item) => `
        <article class="music-list-item">
          <h4>${escapeHtml(item.name)}</h4>
          <p>${escapeHtml(item.info)}</p>
          <p>${escapeHtml(item.url)}</p>
        </article>
      `, "No fetch history yet.");
      updateMusicMetrics();
    }

    async function refreshMusicDownloadHistory() {
      const response = await fetch("/api/music/history/downloads");
      window.__musicDownloadHistory = await response.json();
      renderMusicList("music-download-history-list", window.__musicDownloadHistory, (item) => `
        <article class="music-list-item">
          <h4>${escapeHtml(item.title)}</h4>
          <p>${escapeHtml(item.artists)}${item.album ? ` • ${escapeHtml(item.album)}` : ""}</p>
          <p>${escapeHtml(item.path)}</p>
        </article>
      `, "No download history yet.");
      updateMusicMetrics();
    }

    async function refreshMusicSettings() {
      const response = await fetch("/api/music/settings");
      const settings = await response.json();
      window.__musicSettings = settings;
      const apiUrl = document.getElementById("music-spotfetch-api-url");
      const useApi = document.getElementById("music-use-spotfetch-api");
      const downloadPath = document.getElementById("music-download-path");
      if (apiUrl) apiUrl.value = settings.spotFetchAPIUrl || "";
      if (useApi) useApi.checked = Boolean(settings.useSpotFetchAPI);
      if (downloadPath) downloadPath.value = settings.downloadPath || "";
    }

    function bindMusicEvents() {
      document.querySelectorAll(".music-nav-btn[data-music-panel]").forEach((button) => {
        button.addEventListener("click", () => switchMusicPanel(button.dataset.musicPanel || "fetch"));
      });
      document.getElementById("music-fetch-form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const input = document.getElementById("music-spotify-url");
        const url = input?.value?.trim();
        if (!url) return;
        const response = await fetch("/api/music/metadata/fetch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url })
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          alert(payload.detail || `Request failed (${response.status})`);
          return;
        }
        window.__musicMetadata = await response.json();
        renderMusicMetadata();
        await refreshMusicFetchHistory();
      });
      document.getElementById("music-refresh-queue")?.addEventListener("click", refreshMusicQueue);
      document.getElementById("music-clear-queue")?.addEventListener("click", async () => {
        await fetch("/api/music/queue", { method: "DELETE" });
        await refreshMusicQueue();
      });
      document.getElementById("music-refresh-fetch-history")?.addEventListener("click", refreshMusicFetchHistory);
      document.getElementById("music-clear-fetch-history")?.addEventListener("click", async () => {
        await fetch("/api/music/history/fetch", { method: "DELETE" });
        await refreshMusicFetchHistory();
      });
      document.getElementById("music-refresh-download-history")?.addEventListener("click", refreshMusicDownloadHistory);
      document.getElementById("music-clear-download-history")?.addEventListener("click", async () => {
        await fetch("/api/music/history/downloads", { method: "DELETE" });
        await refreshMusicDownloadHistory();
      });
      document.getElementById("music-settings-form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        await fetch("/api/music/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            values: {
              spotFetchAPIUrl: document.getElementById("music-spotfetch-api-url")?.value?.trim() || "",
              useSpotFetchAPI: !!document.getElementById("music-use-spotfetch-api")?.checked,
              downloadPath: document.getElementById("music-download-path")?.value?.trim() || ""
            }
          })
        });
        await refreshMusicSettings();
      });
    }

    async function refreshMusicState() {
      await Promise.all([
        refreshMusicQueue(),
        refreshMusicFetchHistory(),
        refreshMusicDownloadHistory(),
        refreshMusicSettings()
      ]);
      renderMusicMetadata();
    }

    function renderGeneralSettings() {
      const label = document.getElementById("mullvad-status-label");
      const summary = document.getElementById("mullvad-status-summary");
      const dot = document.getElementById("mullvad-status-dot");
      if (!label || !summary || !dot) {
        return;
      }
      const mullvad = window.__generalSettings?.mullvad;
      if (!mullvad) {
        label.textContent = "Checking Mullvad status...";
        summary.textContent = "";
        dot.className = "settings-status-dot";
        return;
      }
      dot.className = "settings-status-dot " + (mullvad.connected ? "connected" : "disconnected");
      label.textContent = mullvad.connected ? "Connected to Mullvad VPN" : "Not connected to Mullvad VPN";
      summary.textContent = mullvad.summary || "";
    }

    async function refreshGeneralSettings() {
      const response = await fetch("/api/settings/general");
      if (!response.ok) {
        return;
      }
      window.__generalSettings = await response.json();
      renderGeneralSettings();
    }

    async function refreshYouTubeState() {
      const response = await fetch("/api/youtube/state");
      window.__youtubeState = await response.json();
      const input = document.getElementById("youtube-url");
      if (input && window.__youtubeState?.latest_lookup?.source_url) {
        input.value = window.__youtubeState.latest_lookup.source_url;
      }
      if (!window.__youtubeSelected) {
        window.__youtubeSelected = new Set();
      }
      const currentIds = new Set((window.__youtubeState?.latest_lookup?.entries || []).map((entry) => entry.id));
      window.__youtubeSelected = new Set(Array.from(window.__youtubeSelected).filter((id) => currentIds.has(id)));
      renderYouTubeResults();
      renderYouTubeSubscriptions();
    }

    async function refreshBrowserState() {
      const response = await fetch("/api/browser/state");
      const state = await response.json();
      window.__browserState = state;
      updateBrowserButtons(state);
      const streams = state.streams || [];
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
          const title = cleanMediaTitle(metadata.raw_title || state.page_title || "Untitled") || "Untitled";
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
      await refreshYouTubeState();
      await refreshMusicState();
      await refreshGeneralSettings();
    }

    document.getElementById("youtube-lookup-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = document.getElementById("youtube-url");
      const button = document.getElementById("youtube-lookup-btn");
      const url = input?.value?.trim();
      if (!url) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/youtube/lookup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, refresh: false })
        });
        window.__youtubeState = {
          ...(window.__youtubeState || {}),
          latest_lookup: await response.json(),
          subscriptions: window.__youtubeState?.subscriptions || []
        };
        window.__youtubeSelected = new Set();
        renderYouTubeResults();
      } finally {
        button.disabled = false;
      }
    });

    document.getElementById("youtube-refresh-btn")?.addEventListener("click", async () => {
      const input = document.getElementById("youtube-url");
      const url = input?.value?.trim() || window.__youtubeState?.latest_lookup?.source_url;
      if (!url) return;
      const button = document.getElementById("youtube-refresh-btn");
      button.disabled = true;
      try {
        const response = await fetch("/api/youtube/lookup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, refresh: true })
        });
        window.__youtubeState = {
          ...(window.__youtubeState || {}),
          latest_lookup: await response.json(),
          subscriptions: window.__youtubeState?.subscriptions || []
        };
        window.__youtubeSelected = new Set();
        renderYouTubeResults();
      } finally {
        button.disabled = false;
      }
    });

    document.getElementById("youtube-subscribe-btn")?.addEventListener("click", async () => {
      const lookup = window.__youtubeState?.latest_lookup;
      if (!lookup?.cache_key) return;
      const button = document.getElementById("youtube-subscribe-btn");
      button.disabled = true;
      try {
        await fetch("/api/youtube/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cache_key: lookup.cache_key })
        });
        await refreshYouTubeState();
      } finally {
        button.disabled = false;
      }
    });

    document.getElementById("youtube-select-all")?.addEventListener("change", (event) => {
      const lookup = window.__youtubeState?.latest_lookup;
      if (!lookup) return;
      const checked = !!event.target.checked;
      const selected = window.__youtubeSelected || new Set();
      lookup.entries.forEach((entry) => {
        if (entry.status === "downloaded") return;
        if (checked) {
          selected.add(entry.id);
        } else {
          selected.delete(entry.id);
        }
      });
      window.__youtubeSelected = selected;
      renderYouTubeResults();
    });

    document.getElementById("youtube-queue-btn")?.addEventListener("click", async () => {
      const lookup = window.__youtubeState?.latest_lookup;
      const selected = Array.from(window.__youtubeSelected || []);
      if (!lookup?.cache_key || !selected.length) return;
      const button = document.getElementById("youtube-queue-btn");
      button.disabled = true;
      try {
        await fetch("/api/youtube/queue", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cache_key: lookup.cache_key, video_ids: selected })
        });
        window.__youtubeSelected = new Set();
        await refresh();
      } finally {
        button.disabled = false;
      }
    });

    bindMusicEvents();
    renderGeneralSettings();
    refresh();
    setInterval(refresh, 1500);
  </script>
</body>
</html>
"""
