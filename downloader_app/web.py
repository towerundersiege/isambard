from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .download_manager import DownloadManager
from .media_catalog import MediaCatalog
from .metadata_resolver import sanitize_path_segment
from .music_manager import MusicManager
from .music_web import install_music
from .vpn import MullvadGuard
from .youtube_manager import YouTubeManager


EXTENSION_DIR = Path(__file__).resolve().parent.parent / "browser_extension"
LOGGER = logging.getLogger("isambard.web")
BROWSER_SITES = {
    "yflix": "https://yflix.to/",
    "dashflix": "https://dashflix.top/",
}
YFLIX_AUTO_COMMAND_INTERVAL_SECONDS = 25


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


class MediaAutoFindRequest(BaseModel):
    title: str
    year: str = ""
    media_type: str = "movie"
    site: str = "yflix"
    season: int | None = None
    episode: int | None = None
    poster_url: str = ""
    backdrop_url: str = ""


class MediaLocalStatusRequest(BaseModel):
    title: str
    year: str = ""
    media_type: str = "movie"
    season: int | None = None
    episode: int | None = None


def build_app(
    download_manager: DownloadManager,
    youtube_manager: YouTubeManager,
    music_manager: MusicManager,
    mullvad_guard: MullvadGuard,
    media_catalog: MediaCatalog,
) -> FastAPI:
    app = FastAPI(title="Downloader")
    app.mount("/extension", StaticFiles(directory=EXTENSION_DIR), name="extension")
    install_music(app, music_manager)
    app.state.startup_id = str(time.time_ns())
    app.state.browser_state = {
        "page_url": "",
        "page_title": "",
        "metadata": {},
        "streams": [],
        "can_go_back": False,
        "can_go_forward": False,
        "site": "yflix",
    }
    app.state.browser_command_sequence = int(time.time() * 1000)
    app.state.browser_command = {"id": app.state.browser_command_sequence, "action": "", "value": ""}
    app.state.browser_command_queue = []
    app.state.browser_command_next_available_at = 0.0

    def next_browser_command_id() -> int:
        app.state.browser_command_sequence = max(
            int(app.state.browser_command.get("id", 0)),
            int(app.state.browser_command_sequence),
            int(time.time() * 1000),
        ) + 1
        return app.state.browser_command_sequence

    def is_rate_limited_browser_command(command: dict) -> bool:
        value = str(command.get("value") or "")
        return (
            command.get("action") == "navigate"
            and value.startswith(BROWSER_SITES["yflix"])
            and "isambard_title=" in value
        )

    def activate_browser_command(command: dict) -> None:
        app.state.browser_command = command
        if is_rate_limited_browser_command(command):
            app.state.browser_command_next_available_at = time.monotonic() + YFLIX_AUTO_COMMAND_INTERVAL_SECONDS

    def queue_browser_command(action: str, value: str = "") -> dict:
        current_id = next_browser_command_id()
        command = {"id": current_id, "action": action, "value": value}
        delay_remaining = app.state.browser_command_next_available_at - time.monotonic()
        if app.state.browser_command.get("action") or delay_remaining > 0:
            app.state.browser_command_queue.append(command)
            LOGGER.info(
                "queued browser command id=%s action=%s pending=%s delay_remaining=%.1fs",
                current_id,
                action,
                len(app.state.browser_command_queue),
                max(0.0, delay_remaining),
            )
        else:
            activate_browser_command(command)
            LOGGER.info("activated browser command id=%s action=%s", current_id, action)
        return command

    def require_mullvad(context: str) -> None:
        try:
            mullvad_guard.assert_connected(context)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def initial_discover_state() -> dict:
        try:
            return media_catalog.discover("")
        except RuntimeError as exc:
            LOGGER.info("initial discover preload unavailable: %s", exc)
            return {"configured": media_catalog.summary().get("tmdb", {}).get("configured", False), "query": "", "sections": [], "source": "tmdb"}

    def script_json(payload: dict) -> str:
        return json.dumps(payload).replace("<", "\\u003c")

    def render_page(active_page: str, media_view: str = "discover") -> str:
        normalized_media_view = media_view if media_view in {"discover", "search", "download"} else "discover"
        initial_media_state = (
            initial_discover_state()
            if active_page in {"library", "downloads"}
            else {"configured": media_catalog.summary().get("tmdb", {}).get("configured", False), "query": "", "sections": [], "source": "tmdb"}
        )
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
            .replace("__INITIAL_MEDIA_VIEW__", normalized_media_view)
            .replace("__INITIAL_DISCOVER_STATE__", script_json(initial_media_state))
        )

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/downloads", status_code=307)

    @app.get("/downloads", response_class=HTMLResponse)
    def downloads_page() -> str:
        return render_page("downloads")

    @app.get("/movies-tv", response_class=HTMLResponse)
    def movies_tv_page() -> str:
        return render_page("library", "discover")

    @app.get("/movies-tv/discover", response_class=HTMLResponse)
    def movies_tv_discover_page() -> str:
        return render_page("library", "discover")

    @app.get("/movies-tv/search", response_class=HTMLResponse)
    def movies_tv_search_page() -> str:
        return render_page("library", "search")

    @app.get("/movies-tv/download", response_class=HTMLResponse)
    def movies_tv_download_page() -> str:
        return render_page("library", "download")

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
        require_mullvad("Browser capture queueing")
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

    @app.post("/api/tasks/{task_id}/pause")
    def pause_task(task_id: str) -> dict:
        LOGGER.info("api pause task id=%s", task_id)
        task = download_manager.pause_task(task_id)
        if task is None:
            LOGGER.warning("pause task failed id=%s reason=not_found", task_id)
            return {"ok": False, "error": "task not found"}
        LOGGER.info("task pause result id=%s status=%s", task_id, task.status)
        return {"ok": True, "task": task.to_dict()}

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(task_id: str) -> dict:
        LOGGER.info("api resume task id=%s", task_id)
        task = download_manager.resume_task(task_id)
        if task is None:
            LOGGER.warning("resume task failed id=%s reason=not_found", task_id)
            return {"ok": False, "error": "task not found"}
        LOGGER.info("task resume result id=%s status=%s", task_id, task.status)
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
        command = app.state.browser_command
        command_value = str(command.get("value") or "")
        if (
            command.get("action") == "navigate"
            and command_value
            and _browser_target_matches(command_value, request.page_url)
            and request.page_url
        ):
            app.state.browser_command = {"id": command.get("id", 0), "action": "", "value": ""}
            LOGGER.info("cleared browser navigate command id=%s from browser state", command.get("id", 0))
        return {"ok": True}

    @app.get("/api/browser/command")
    def browser_command() -> dict:
        if not app.state.browser_command.get("action") and app.state.browser_command_queue:
            delay_remaining = app.state.browser_command_next_available_at - time.monotonic()
            if delay_remaining > 0:
                return app.state.browser_command
            activate_browser_command(app.state.browser_command_queue.pop(0))
            LOGGER.info(
                "activated queued browser command id=%s action=%s remaining=%s",
                app.state.browser_command.get("id", 0),
                app.state.browser_command.get("action", ""),
                len(app.state.browser_command_queue),
            )
        return app.state.browser_command

    @app.post("/api/browser/command/ack")
    def acknowledge_browser_command(request: BrowserCommandRequest) -> dict:
        if int(app.state.browser_command.get("id", 0)) == request.command_id:
            app.state.browser_command = {"id": request.command_id, "action": "", "value": ""}
            LOGGER.info("acknowledged browser command id=%s", request.command_id)
        return {"ok": True}

    @app.post("/api/browser/command/{action}")
    def issue_browser_command(action: str) -> dict:
        require_mullvad("Browser navigation")
        if action not in {"back", "forward", "reload"}:
            LOGGER.warning("unsupported browser command action=%s", action)
            return {"ok": False, "error": "unsupported action"}
        command = queue_browser_command(action)
        LOGGER.info("issued browser command id=%s action=%s", command["id"], action)
        return {"ok": True, "id": command["id"], "action": action}

    @app.post("/api/browser/navigate")
    def navigate_browser(request: BrowserNavigateRequest) -> dict:
        require_mullvad("Browser navigation")
        target = BROWSER_SITES.get(request.site)
        if not target:
            LOGGER.warning("unsupported browser site site=%s", request.site)
            return {"ok": False, "error": "unsupported site"}
        command = queue_browser_command("navigate", target)
        LOGGER.info("issued browser navigate id=%s site=%s target=%s", command["id"], request.site, target)
        return {"ok": True, "id": command["id"], "action": "navigate", "value": target}

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
        return {"mullvad": mullvad_guard.status(force=True)}

    @app.get("/api/system/summary")
    def system_summary() -> dict:
        usage = shutil.disk_usage(download_manager.downloads_dir)
        return {
            "startup_id": app.state.startup_id,
            "downloads_path": str(download_manager.downloads_dir),
            "mullvad": mullvad_guard.status(force=True),
            "disk": {
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
            },
            **media_catalog.summary(),
        }

    @app.get("/api/media/discover")
    def media_discover(query: str = "") -> dict:
        try:
            return media_catalog.discover(query)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/media/details")
    def media_details(provider: str, provider_id: str, media_type: str = "movie") -> dict:
        try:
            return media_catalog.details(provider, provider_id, media_type)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/media/local-status")
    def media_local_status(request: MediaLocalStatusRequest) -> dict:
        status = _local_media_status(download_manager.downloads_dir, request)
        if status.get("exists"):
            status["jellyfin_url"] = media_catalog.jellyfin_search_url(request.title)
        return status

    @app.post("/api/media/auto-find")
    def media_auto_find(request: MediaAutoFindRequest) -> dict:
        require_mullvad("Browser auto-find")
        payload = media_catalog.auto_find_payload(
            request.title,
            year=request.year,
            media_type=request.media_type,
            site=request.site,
            season=request.season,
            episode=request.episode,
            poster_url=request.poster_url,
            backdrop_url=request.backdrop_url,
        )
        command = queue_browser_command("navigate", payload["target_url"])
        LOGGER.info(
            "issued browser auto-find id=%s title=%s site=%s target=%s",
            command["id"],
            request.title,
            request.site,
            payload["target_url"],
        )
        return {
            **payload,
            "browser_command_id": command["id"],
        }

    @app.post("/api/youtube/lookup")
    def youtube_lookup(request: YouTubeLookupRequest) -> dict:
        require_mullvad("YouTube lookup")
        LOGGER.info("youtube lookup url=%s refresh=%s", request.url, request.refresh)
        lookup = youtube_manager.lookup(request.url, request.refresh)
        return lookup.to_dict()

    @app.post("/api/youtube/queue")
    def youtube_queue(request: YouTubeQueueRequest) -> dict:
        require_mullvad("YouTube queueing")
        LOGGER.info("youtube queue cache_key=%s count=%s", request.cache_key, len(request.video_ids))
        queued = youtube_manager.queue_selected(request.cache_key, request.video_ids)
        return {"ok": True, "queued": queued}

    @app.post("/api/youtube/subscribe")
    def youtube_subscribe(request: YouTubeSubscribeRequest) -> dict:
        require_mullvad("YouTube subscriptions")
        subscription = youtube_manager.subscribe(request.cache_key)
        LOGGER.info("youtube subscribed cache_key=%s subscription_id=%s", request.cache_key, subscription["id"])
        return {"ok": True, "subscription": subscription}

    @app.post("/api/youtube/subscriptions/{subscription_id}/refresh")
    def refresh_youtube_subscription(subscription_id: str) -> dict:
        require_mullvad("YouTube subscription refresh")
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


def _browser_target_matches(command_url: str, page_url: str) -> bool:
    if not command_url or not page_url:
        return False
    command = urlsplit(command_url)
    page = urlsplit(page_url)
    return (
        command.scheme.lower() == page.scheme.lower()
        and command.netloc.lower() == page.netloc.lower()
        and (command.path or "/") == (page.path or "/")
        and sorted(parse_qsl(command.query, keep_blank_values=True))
        == sorted(parse_qsl(page.query, keep_blank_values=True))
        and command.fragment == page.fragment
    )


def _local_media_status(downloads_dir: Path, request: MediaLocalStatusRequest) -> dict:
    title = (request.title or "").strip()
    year = (request.year or "").strip()
    display_title = f"{title} ({year})" if title and year and f"({year})" not in title else title
    media_type = (request.media_type or "movie").strip().lower()
    if not display_title:
        return {"exists": False, "path": ""}

    if media_type == "tv":
        show_dir = downloads_dir / "tv" / sanitize_path_segment(display_title)
        if request.season and request.episode:
            filename = f"{display_title} - S{request.season:02d}E{request.episode:02d}.mp4"
            path = (
                show_dir
                / sanitize_path_segment(f"{display_title} - S{request.season:02d}")
                / sanitize_path_segment(filename)
            )
            return {"exists": path.is_file(), "path": str(path) if path.exists() else ""}
        if request.season:
            season_dir = show_dir / sanitize_path_segment(f"{display_title} - S{request.season:02d}")
            exists = season_dir.is_dir() and any(season_dir.glob("*.mp4"))
            return {"exists": exists, "path": str(season_dir) if season_dir.exists() else ""}
        exists = show_dir.is_dir() and any(show_dir.glob("*/*.mp4"))
        return {"exists": exists, "path": str(show_dir) if show_dir.exists() else ""}

    movie_path = (
        downloads_dir
        / "movies"
        / sanitize_path_segment(display_title)
        / f"{sanitize_path_segment(display_title)}.mp4"
    )
    return {"exists": movie_path.is_file(), "path": str(movie_path) if movie_path.exists() else ""}


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
      width: 100vw;
      margin: 0;
      padding-bottom: 0;
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
    .settings-storage {
      display: grid;
      gap: 12px;
    }
    .settings-storage-top {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 12px;
    }
    .settings-storage-value {
      font-size: 32px;
      font-weight: 800;
      letter-spacing: -0.05em;
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
      border-radius: 16px;
      overflow: hidden;
      backdrop-filter: blur(18px);
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.28);
    }
    .panel-header {
      padding: 10px 16px;
      min-height: 56px;
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
      position: relative;
      background: rgba(8, 14, 24, 0.92);
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
    .browser-frame.is-hidden {
      visibility: hidden;
    }
    .browser-frame:focus {
      outline: 2px solid rgba(15,123,255,0.9);
      outline-offset: -2px;
    }
    .browser-blocked {
      position: absolute;
      inset: 0;
      display: none;
      place-items: center;
      padding: 24px;
      text-align: center;
      background:
        radial-gradient(circle at 50% 0%, rgba(239,68,68,0.14), transparent 35%),
        linear-gradient(180deg, rgba(7,16,25,0.92), rgba(7,16,25,0.98));
    }
    .browser-blocked.is-visible {
      display: grid;
    }
    .browser-blocked-card {
      max-width: 420px;
      display: grid;
      gap: 12px;
      justify-items: center;
    }
    .browser-blocked-icon {
      width: 72px;
      height: 72px;
      border-radius: 22px;
      display: grid;
      place-items: center;
      font-size: 28px;
      color: #ffb6ae;
      background: rgba(239,68,68,0.12);
      border: 1px solid rgba(239,68,68,0.24);
    }
    .browser-blocked-title {
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.03em;
    }
    .browser-blocked-copy {
      color: var(--text-muted);
      line-height: 1.5;
      font-size: 14px;
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
    :root {
      --bg-base: #071019;
      --surface-1: rgba(13, 23, 38, 0.9);
      --surface-2: rgba(16, 29, 49, 0.92);
      --surface-3: rgba(255,255,255,0.04);
      --surface-4: rgba(255,255,255,0.02);
      --text-strong: #f4f7fb;
      --text-muted: #8fa3bf;
      --border-soft: rgba(255,255,255,0.08);
      --accent-blue: #1da1ff;
      --accent-teal: #00d4b8;
      --accent-violet: #8b5cf6;
      --accent-green: #22c55e;
      --warning: #f59e0b;
      --danger: #ef4444;
      --radius-lg: 24px;
      --radius-md: 18px;
      --shadow-lg: 0 24px 80px rgba(0, 0, 0, 0.28);
    }
    body {
      min-height: 100vh;
      color: var(--text-strong);
      background:
        radial-gradient(circle at 78% 10%, rgba(29,161,255,0.16), transparent 22%),
        radial-gradient(circle at 56% 0%, rgba(139,92,246,0.13), transparent 18%),
        radial-gradient(circle at 0% 100%, rgba(0,212,184,0.10), transparent 22%),
        #071019;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.22;
      background-image:
        linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
      background-size: 28px 28px;
      mask-image: radial-gradient(circle at center, black 30%, transparent 88%);
    }
    .mobile-nav-toggle {
      position: fixed;
      top: 16px;
      right: 16px;
      z-index: 40;
      display: none;
      border: 1px solid var(--border-soft);
      border-radius: 14px;
      padding: 10px 14px;
      background: rgba(13, 23, 38, 0.96);
      color: var(--text-strong);
      box-shadow: var(--shadow-lg);
    }
    .shell.shell-premium {
      width: 100%;
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 0;
      align-items: start;
    }
    .sidebar,
    .panel,
    .sidebar-card {
      background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.02));
      border: 1px solid var(--border-soft);
      backdrop-filter: blur(18px);
      box-shadow: var(--shadow-lg);
    }
    .sidebar {
      position: fixed;
      top: 0;
      left: 0;
      bottom: 0;
      width: 244px;
      min-height: 100vh;
      border-radius: 0;
      padding: 18px 16px 16px;
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 16px;
      align-self: start;
      overflow-y: auto;
    }
    .sidebar.is-collapsed {
      width: 84px;
      padding-inline: 16px;
    }
    .sidebar.is-collapsed .brand-meta,
    .sidebar.is-collapsed .nav-label,
    .sidebar.is-collapsed .nav-item span:last-child,
    .sidebar.is-collapsed .sidebar-collapse-label {
      display: none;
    }
    .sidebar.is-collapsed .sidebar-nav {
      justify-items: stretch;
    }
    .brand-block {
      display: flex;
      gap: 14px;
      align-items: center;
      justify-content: flex-start;
    }
    .brand-meta {
      display: grid;
      gap: 8px;
      min-width: 0;
    }
    .brand-mark {
      width: 52px;
      height: 52px;
      border-radius: 18px;
      display: grid;
      place-items: center;
      font-size: 20px;
      font-weight: 800;
      color: white;
      background: linear-gradient(135deg, var(--accent-blue), var(--accent-violet));
      box-shadow: 0 12px 30px rgba(29,161,255,0.3);
    }
    .brand-title {
      margin: 0;
      font-size: 24px;
      letter-spacing: -0.03em;
    }
    .sidebar-collapse-btn {
      width: auto;
      height: auto;
      border-radius: 0;
      border: 0;
      background: transparent;
      color: inherit;
      display: flex;
      place-items: unset;
      cursor: pointer;
      flex: 0 0 auto;
      box-shadow: none;
      padding: 0;
      font: inherit;
      text-align: left;
    }
    .sidebar-collapse-btn:hover {
      color: var(--text-strong);
      background: transparent;
    }
    .nav-section {
      display: grid;
      gap: 8px;
    }
    .nav-label,
    .stat-label,
    .eyebrow,
    .sidebar-card-label {
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }
    .sidebar-nav {
      display: grid;
      gap: 6px;
    }
    .nav-item {
      display: flex;
      gap: 12px;
      align-items: center;
      padding: 10px 6px 10px 11px;
      border-radius: 12px;
      text-decoration: none;
      color: var(--text-muted);
      background: transparent;
      border: 0;
      transition: color 180ms ease, transform 180ms ease;
    }
    .sidebar.is-collapsed .nav-item {
      justify-content: flex-start;
      width: 100%;
      padding-inline: 11px 6px;
    }
    .nav-item:hover,
    .nav-item[aria-selected="true"] {
      color: var(--text-strong);
      transform: translateY(-1px);
    }
    .nav-item[aria-selected="true"] {
      font-weight: 800;
    }
    .nav-item-passive {
      opacity: 0.68;
      cursor: default;
    }
    .nav-icon {
      width: 30px;
      height: 30px;
      border-radius: 8px;
      display: inline-grid;
      place-items: center;
      color: currentColor;
      background: transparent;
      flex: 0 0 auto;
    }
    .nav-item[aria-selected="true"] .nav-icon {
      color: var(--accent-blue);
    }
    .sidebar-collapse-btn .nav-icon {
      color: currentColor;
    }
    .nav-icon svg,
    .utility-chip-icon svg,
    .storage-card-icon svg,
    .brand-mark svg {
      width: 20px;
      height: 20px;
      display: block;
      stroke: currentColor;
      fill: none;
      stroke-width: 1.8;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .sidebar-footer {
      align-self: end;
      display: grid;
      gap: 8px;
      position: sticky;
      bottom: 0;
      padding-top: 16px;
      background: transparent;
    }
    .sidebar-footer-bottom {
      display: grid;
      gap: 8px;
      justify-items: stretch;
    }
    .mullvad-banner {
      display: none;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 16px;
      border-radius: 16px;
      border: 1px solid rgba(239,68,68,0.24);
      background: rgba(239,68,68,0.12);
      color: #ffd2cc;
    }
    .mullvad-banner.is-visible {
      display: flex;
    }
    .mullvad-banner-title {
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0.02em;
    }
    .mullvad-banner-copy {
      color: rgba(255,210,204,0.86);
      font-size: 12px;
      line-height: 1.45;
    }
    .storage-card-icon {
      width: 28px;
      height: 28px;
      border-radius: 10px;
      display: inline-grid;
      place-items: center;
      color: var(--text-strong);
      background: rgba(255,255,255,0.08);
    }
    .storage-progress {
      width: 100%;
      height: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.07);
      overflow: hidden;
    }
    .storage-progress > span {
      display: block;
      height: 100%;
      width: 0%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent-blue), var(--accent-teal));
      box-shadow: 0 0 18px rgba(29,161,255,0.28);
    }
    .storage-progress-label {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--text-muted);
      font-size: 12px;
    }
    .sidebar-card strong {
      font-size: 16px;
      letter-spacing: -0.02em;
    }
    .sidebar-card span:last-child {
      color: var(--text-muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .app-main {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 12px;
      min-width: 0;
      margin-left: 244px;
      padding: 8px 10px 10px;
    }
    body.sidebar-collapsed .app-main {
      margin-left: 84px;
    }
    .search-input {
      width: 100%;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 16px;
      padding: 14px 16px;
      background: rgba(255,255,255,0.04);
      color: var(--text-strong);
      font-size: 14px;
    }
    .search-input:focus,
    .youtube-input:focus,
    .music-input:focus,
    .browser-site-select:focus,
    .youtube-btn:focus,
    .music-btn:focus,
    .browser-btn:focus {
      outline: 2px solid rgba(29,161,255,0.8);
      outline-offset: 2px;
    }
    .panel-toolbar,
    .quick-actions {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .page {
      display: none;
    }
    .page.is-active {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 10px;
      min-width: 0;
    }
    .page-hero {
      position: relative;
      overflow: hidden;
      border-radius: 16px;
      padding: 26px 28px;
      background:
        radial-gradient(circle at 70% 18%, rgba(29,161,255,0.26), transparent 18%),
        radial-gradient(circle at 83% 18%, rgba(139,92,246,0.28), transparent 16%),
        linear-gradient(135deg, rgba(10, 27, 52, 0.92), rgba(12, 22, 39, 0.86));
      border: 1px solid rgba(255,255,255,0.08);
      box-shadow: var(--shadow-lg);
    }
    .page-hero::after {
      content: "";
      position: absolute;
      top: 18px;
      right: 88px;
      width: 320px;
      height: 72px;
      background:
        radial-gradient(circle at 20% 50%, rgba(0,212,184,0.7), transparent 18%),
        radial-gradient(circle at 55% 46%, rgba(29,161,255,0.92), transparent 16%),
        radial-gradient(circle at 82% 50%, rgba(168,85,247,0.96), transparent 18%);
      filter: blur(18px);
      opacity: 0.95;
      pointer-events: none;
    }
    .page-title {
      margin: 0;
      font-size: clamp(32px, 3vw, 40px);
      letter-spacing: -0.04em;
    }
    .page-copy,
    .section-copy,
    .stat-meta {
      margin: 8px 0 0;
      color: var(--text-muted);
      line-height: 1.5;
      font-size: 13px;
    }
    .page-header-row {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 18px;
      flex-wrap: wrap;
    }
    .page-header-actions,
    .page-filter-pills {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .page-hero-tools {
      display: grid;
      gap: 14px;
      margin-top: 18px;
    }
    .page-action-btn,
    .page-filter-pill {
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px;
      padding: 10px 12px;
      background: rgba(17, 29, 48, 0.72);
      color: var(--text-muted);
      font-size: 12px;
      font-weight: 700;
      font-family: inherit;
    }
    button.page-action-btn,
    button.page-filter-pill {
      cursor: pointer;
    }
    .page-action-btn.is-active,
    .page-filter-pill.is-active {
      color: var(--text-strong);
      border-color: rgba(29,161,255,0.3);
      background: rgba(29,161,255,0.14);
    }
    .downloads-page-layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }
    .downloads-main-column {
      display: grid;
      gap: 12px;
    }
    .downloads-main {
      display: grid;
      gap: 12px;
    }
    .library-layout {
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
      align-items: start;
    }
    .library-main,
    .library-side,
    .library-sections {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 16px;
      min-width: 0;
    }
    .library-content-stage,
    .downloads-main-column,
    .downloads-main,
    .library-browser-stage {
      min-width: 0;
    }
    .downloads-filter-bar {
      padding: 14px 16px 0;
      display: grid;
      gap: 12px;
    }
    .downloads-filter-group {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .downloads-filter-label {
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      min-width: 46px;
    }
    .downloads-filter-pill {
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 999px;
      padding: 8px 12px;
      background: rgba(255,255,255,0.04);
      color: var(--text-muted);
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      font-family: inherit;
    }
    .downloads-filter-pill.is-active {
      color: var(--text-strong);
      border-color: rgba(29,161,255,0.32);
      background: rgba(29,161,255,0.14);
    }
    .library-browser-stage {
      display: none;
      gap: 16px;
    }
    .library-layout.is-download-view .library-browser-stage {
      display: grid;
      grid-template-columns: minmax(0, 1.85fr) minmax(320px, 0.7fr);
      align-items: start;
    }
    .auto-download-panel {
      grid-column: 1 / -1;
    }
    .library-layout.is-download-view .library-content-stage {
      display: none;
    }
    .library-highlight-panel {
      overflow: hidden;
    }
    .library-highlight {
      height: 332px;
      padding: 26px;
      display: grid;
      grid-template-rows: minmax(0, 1fr) auto;
      gap: 12px;
      background:
        linear-gradient(180deg, rgba(7, 16, 26, 0.08), rgba(7, 16, 26, 0.92)),
        radial-gradient(circle at 72% 22%, rgba(132, 76, 255, 0.38), transparent 18%),
        radial-gradient(circle at 52% 20%, rgba(14, 160, 255, 0.22), transparent 16%),
        radial-gradient(circle at 35% 16%, rgba(22, 198, 171, 0.2), transparent 15%),
        linear-gradient(135deg, #0f2034, #0b1625);
      background-size: cover;
      background-position: center;
    }
    .library-highlight-stage {
      align-self: end;
      display: grid;
      gap: 12px;
      align-content: end;
      cursor: pointer;
    }
    .library-highlight-copy-group {
      display: grid;
      gap: 12px;
      align-content: end;
    }
    .library-highlight-stage.is-animating {
      animation: libraryHighlightSlide 320ms ease;
    }
    @keyframes libraryHighlightSlide {
      from {
        opacity: 0.25;
        transform: translateX(18px);
      }
      to {
        opacity: 1;
        transform: translateX(0);
      }
    }
    .library-highlight-title {
      margin: 0;
      font-size: clamp(34px, 4vw, 48px);
      line-height: 1.02;
      letter-spacing: 0;
      max-width: min(760px, 68vw);
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .library-highlight-copy {
      margin: 0;
      color: rgba(232, 238, 247, 0.82);
      line-height: 1.55;
      font-size: 14px;
      max-width: 74ch;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .library-highlight-controls {
      display: flex;
      gap: 8px;
      align-items: center;
      justify-content: center;
      margin-top: 6px;
    }
    .library-highlight-dots {
      display: flex;
      gap: 8px;
      align-items: center;
      justify-content: center;
      flex: 1 1 auto;
    }
    .library-highlight-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: rgba(255,255,255,0.3);
      transition: transform 160ms ease, background 160ms ease;
    }
    .library-highlight-dot.is-active {
      background: #ffffff;
      transform: scale(1.2);
    }
    .library-highlight-arrow {
      width: auto;
      height: auto;
      border: 0;
      background: transparent;
      color: var(--text-strong);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 22px;
      line-height: 1;
      padding: 0 2px;
    }
    .library-highlight-meta {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .library-highlight-meta .pill {
      background: rgba(7, 16, 26, 0.58);
    }
    .library-section {
      border-radius: 24px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(13, 24, 37, 0.92);
      overflow: hidden;
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.22);
    }
    .media-search-form {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
    }
    .media-search-input {
      min-width: 0;
    }
    .media-rail {
      padding: 20px 20px 24px;
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: minmax(200px, 1fr);
      gap: 18px;
      overflow-x: auto;
      scrollbar-width: auto;
      position: relative;
    }
    .media-rail::-webkit-scrollbar {
      height: 10px;
    }
    .media-rail::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.14);
      border-radius: 999px;
    }
    .media-rail::-webkit-scrollbar-track {
      background: rgba(255,255,255,0.05);
      border-radius: 999px;
    }
    .media-grid {
      padding: 18px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 180px));
      gap: 14px;
      justify-content: start;
    }
    .media-card {
      border-radius: 20px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.03);
      display: grid;
      gap: 0;
      min-width: 0;
      transition: transform 180ms ease, border-color 180ms ease, background 180ms ease;
    }
    .media-card:hover {
      transform: translateY(-2px);
      border-color: rgba(255,255,255,0.16);
      background: rgba(255,255,255,0.045);
    }
    .media-card-poster {
      position: relative;
      aspect-ratio: 0.68;
      background: linear-gradient(180deg, rgba(23, 40, 62, 0.92), rgba(11, 20, 31, 0.98));
      overflow: hidden;
    }
    .media-card-poster img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .media-card-poster::after {
      content: "";
      position: absolute;
      inset: auto 0 0;
      height: 44%;
      background: linear-gradient(180deg, rgba(8, 14, 24, 0), rgba(8, 14, 24, 0.9));
    }
    .media-card-badge {
      position: absolute;
      top: 12px;
      left: 12px;
      z-index: 1;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(6, 14, 24, 0.72);
      border: 1px solid rgba(255,255,255,0.08);
      color: #dce8f8;
      font-size: 11px;
      font-weight: 700;
    }
    .media-card-body {
      padding: 14px;
      display: grid;
      gap: 6px;
    }
    .media-card-title {
      margin: 0;
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
    }
    .media-card-meta {
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      color: var(--text-muted);
      font-size: 12px;
    }
    .media-card-actions {
      position: absolute;
      right: 12px;
      bottom: 12px;
      z-index: 2;
      display: flex;
      gap: 8px;
    }
    .media-card-icon-btn {
      width: 40px;
      height: 40px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.06);
      color: var(--text-strong);
      display: inline-grid;
      place-items: center;
      cursor: pointer;
      font-family: inherit;
      box-shadow: 0 10px 22px rgba(0,0,0,0.24);
    }
    .media-card-icon-btn svg {
      width: 20px;
      height: 20px;
      stroke: currentColor;
      fill: none;
      stroke-width: 1.9;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .media-card-btn,
    .media-card-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.06);
      color: var(--text-strong);
      font-size: 12px;
      font-weight: 700;
      padding: 0 12px;
      text-decoration: none;
      cursor: pointer;
      font-family: inherit;
    }
    .media-card-btn.primary {
      background: rgba(15,123,255,0.18);
      border-color: rgba(15,123,255,0.28);
      color: #dfeeff;
    }
    .media-card-empty {
      padding: 22px 18px;
      color: var(--text-muted);
    }
    .auto-request-list {
      display: grid;
      gap: 8px;
      padding: 16px;
      max-height: min(360px, 42vh);
      overflow: auto;
      align-content: start;
    }
    .auto-request-item {
      border: 1px solid rgba(255,255,255,0.07);
      border-radius: 10px;
      background: rgba(255,255,255,0.03);
      overflow: hidden;
    }
    .auto-request-top {
      display: grid;
      grid-template-columns: 158px minmax(0, 1fr) auto 28px;
      align-items: center;
      gap: 12px;
      padding: 10px 12px;
      cursor: pointer;
      list-style: none;
    }
    .auto-request-top::-webkit-details-marker {
      display: none;
    }
    .auto-request-time {
      color: var(--text-muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }
    .auto-request-title {
      min-width: 0;
      color: var(--text-strong);
      font-size: 13px;
      font-weight: 800;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .auto-request-delete {
      width: 28px;
      height: 28px;
      border-radius: 8px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04);
      color: var(--text-muted);
      cursor: pointer;
      display: inline-grid;
      place-items: center;
      font-size: 18px;
      line-height: 1;
    }
    .auto-request-delete:hover {
      color: var(--text-strong);
      background: rgba(239,68,68,0.14);
      border-color: rgba(239,68,68,0.28);
    }
    .auto-request-log {
      margin: 0;
      max-height: 160px;
      overflow: auto;
      border-top: 1px solid rgba(255,255,255,0.05);
      background: rgba(0,0,0,0.24);
      color: #c4d4e6;
      padding: 10px 12px;
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 11px;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .auto-download-toasts {
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 90;
      display: grid;
      gap: 10px;
      width: min(360px, calc(100vw - 36px));
      pointer-events: none;
    }
    .auto-download-toast {
      position: relative;
      overflow: hidden;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.1);
      background: rgba(12, 22, 36, 0.96);
      box-shadow: 0 22px 60px rgba(0,0,0,0.38);
      padding: 14px 42px 14px 14px;
      display: grid;
      gap: 6px;
      pointer-events: auto;
    }
    .auto-download-toast.has-link {
      cursor: pointer;
    }
    .auto-download-progress {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      height: 3px;
      background: linear-gradient(90deg, #16c6ab, #1da1ff);
      transform-origin: left center;
      animation-name: autoDownloadToastProgress;
      animation-timing-function: linear;
      animation-fill-mode: forwards;
    }
    @keyframes autoDownloadToastProgress {
      from { transform: scaleX(1); }
      to { transform: scaleX(0); }
    }
    .download-media-card.is-highlighted,
    .completed-list-item.is-highlighted {
      border-color: rgba(29, 161, 255, 0.62);
      box-shadow: 0 0 0 1px rgba(29, 161, 255, 0.34), 0 24px 70px rgba(29, 161, 255, 0.18);
    }
    .auto-download-close {
      position: absolute;
      top: 8px;
      right: 8px;
      width: 26px;
      height: 26px;
      border: 0;
      border-radius: 9px;
      background: rgba(255,255,255,0.06);
      color: var(--text-muted);
      cursor: pointer;
      font: inherit;
      line-height: 1;
    }
    .auto-download-close:hover {
      color: var(--text-strong);
      background: rgba(255,255,255,0.1);
    }
    .auto-download-title {
      color: var(--text-strong);
      font-size: 14px;
      font-weight: 800;
      line-height: 1.25;
    }
    .auto-download-copy {
      color: var(--text-muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .media-modal {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 28px;
      background: rgba(4, 8, 14, 0.72);
      backdrop-filter: blur(12px);
      z-index: 60;
    }
    .media-modal.is-open {
      display: flex;
    }
    .media-modal-card {
      width: min(980px, 100%);
      max-height: min(88vh, 920px);
      overflow: auto;
      border-radius: 28px;
      border: 1px solid rgba(255,255,255,0.08);
      background: linear-gradient(180deg, rgba(12, 22, 36, 0.98), rgba(9, 17, 28, 0.98));
      box-shadow: 0 40px 120px rgba(0, 0, 0, 0.42);
    }
    .media-modal-hero {
      min-height: 280px;
      padding: 26px;
      display: grid;
      align-content: end;
      gap: 12px;
      background:
        linear-gradient(180deg, rgba(7,16,26,0.1), rgba(7,16,26,0.96)),
        linear-gradient(135deg, #0f2034, #0b1625);
      background-size: cover;
      background-position: center;
    }
    .media-modal-body {
      padding: 24px 26px 26px;
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      gap: 22px;
      align-items: start;
    }
    .media-modal-poster {
      width: 100%;
      aspect-ratio: 0.72;
      border-radius: 22px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04);
    }
    .media-modal-poster img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .media-modal-copy {
      display: grid;
      gap: 14px;
    }
    .media-modal-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: nowrap;
      width: 100%;
    }
    .media-modal-meta .library-highlight-meta {
      flex: 1 1 auto;
      min-width: 0;
    }
    .media-modal-title {
      margin: 0;
      font-size: clamp(28px, 4vw, 42px);
      line-height: 1.02;
      letter-spacing: -0.05em;
    }
    .media-modal-overview {
      margin: 0;
      color: var(--text-muted);
      line-height: 1.65;
      font-size: 14px;
    }
    .media-modal-actions {
      width: auto;
      flex: 0 0 auto;
    }
    .media-modal-action-icon {
      width: 44px;
      height: 44px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.06);
      color: var(--text-strong);
      display: inline-grid;
      place-items: center;
      cursor: pointer;
      text-decoration: none;
    }
    .media-modal-action-icon.is-task-status {
      width: auto;
      min-width: 96px;
      padding: 0 13px;
      grid-auto-flow: column;
      gap: 8px;
      font-size: 12px;
      font-weight: 800;
    }
    .media-modal-action-icon svg {
      width: 20px;
      height: 20px;
      stroke: currentColor;
      fill: none;
      stroke-width: 1.9;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .media-modal-action-icon.is-busy {
      cursor: progress;
    }
    .media-modal-action-icon:disabled {
      opacity: 0.82;
      cursor: progress;
    }
    .media-modal-action-icon .spinner-icon {
      animation: modal-spinner 0.8s linear infinite;
    }
    @keyframes modal-spinner {
      to { transform: rotate(360deg); }
    }
    .media-modal-action-icon.is-owned .jellyfin-dashboard-icon {
      width: 22px;
      height: 22px;
      stroke: none;
      fill: initial;
    }
    .media-detail-list {
      display: grid;
      gap: 10px;
      width: 100%;
    }
    .media-detail-season {
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 16px;
      background: rgba(255,255,255,0.03);
    }
    .media-detail-season {
      padding: 10px 14px;
      display: grid;
      gap: 0;
      width: 100%;
    }
    .media-detail-season[open] {
      gap: 8px;
    }
    .media-detail-season summary {
      list-style: none;
      cursor: pointer;
    }
    .media-detail-season summary::-webkit-details-marker {
      display: none;
    }
    .media-detail-arrow {
      width: 16px;
      height: 16px;
      color: var(--text-strong);
      flex: 0 0 auto;
    }
    .media-detail-arrow path {
      fill: none;
      stroke: currentColor;
      stroke-width: 2.25;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .media-detail-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .media-detail-row-main {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }
    .media-detail-row-actions {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      flex: 0 0 auto;
    }
    .media-detail-count {
      color: var(--text-muted);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .media-detail-title {
      font-size: 14px;
      font-weight: 700;
    }
    .media-detail-episodes {
      display: grid;
      gap: 0;
      width: 100%;
    }
    .media-detail-episode-row {
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 0;
      border-top: 1px solid rgba(255,255,255,0.06);
    }
    .media-detail-episode-title {
      color: var(--text-strong);
      font-size: 12px;
    }
    .media-detail-date {
      color: var(--text-muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .media-detail-empty {
      color: var(--text-muted);
      font-size: 13px;
      line-height: 1.5;
    }
    .media-modal-close {
      position: absolute;
      top: 16px;
      right: 16px;
      width: 40px;
      height: 40px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(6,14,24,0.72);
      color: var(--text-strong);
      display: inline-grid;
      place-items: center;
      font-size: 18px;
      cursor: pointer;
    }
    .downloads-tab-only,
    .library-search-only {
      display: none;
    }
    .premium-panel {
      border-radius: 16px;
      overflow: hidden;
    }
    .premium-header {
      align-items: center;
      border-bottom-color: rgba(255,255,255,0.06);
    }
    .premium-header > div:first-child {
      display: grid;
      align-content: center;
      gap: 2px;
    }
    .section-title {
      margin: 0;
      font-size: 20px;
      line-height: 1.15;
      letter-spacing: -0.03em;
      text-transform: none;
    }
    .download-card-grid {
      padding: 18px;
      display: grid;
      gap: 14px;
    }
    .download-card-grid {
      grid-template-columns: 1fr;
    }
    .download-media-card,
    .activity-item {
      border-radius: 20px;
      border: 1px solid rgba(255,255,255,0.07);
      background: rgba(255,255,255,0.03);
      transition: transform 180ms ease, border-color 180ms ease, background 180ms ease;
    }
    .download-media-card:hover,
    .activity-item:hover {
      transform: translateY(-2px);
      border-color: rgba(255,255,255,0.14);
      background: rgba(255,255,255,0.045);
    }
    .download-media-card {
      padding: 0;
      display: grid;
      grid-template-columns: 168px minmax(0, 1fr);
      gap: 0;
      overflow: hidden;
      min-height: 180px;
      align-items: start;
      contain: layout paint;
    }
    .active-card-poster {
      width: 168px;
      height: 208px;
      min-height: 0;
      padding: 0;
      border-right: 1px solid rgba(255,255,255,0.06);
      background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02));
      display: flex;
      align-items: stretch;
      justify-content: stretch;
      align-self: start;
      overflow: hidden;
    }
    .active-card-main {
      padding: 16px 18px;
      display: grid;
      gap: 14px;
      min-width: 0;
      align-content: start;
    }
    .active-card-top {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
    }
    .active-card-body {
      display: grid;
      gap: 10px;
      min-width: 0;
    }
    .active-card-progress-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
    }
    .active-card-progress-tools {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      justify-self: end;
    }
    .active-card-progress-tools strong {
      min-width: 36px;
      text-align: right;
      font-variant-numeric: tabular-nums;
    }
    .active-card-metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(92px, 1fr));
      gap: 18px;
      align-items: center;
      color: var(--text-muted);
      font-size: 12px;
    }
    .active-card-metrics span {
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .media-cover,
    .completed-cover {
      border-radius: 18px;
      background: linear-gradient(145deg, rgba(29,161,255,0.36), rgba(139,92,246,0.26));
      border: 1px solid rgba(255,255,255,0.08);
      display: grid;
      place-items: end start;
      padding: 10px;
      overflow: hidden;
      position: relative;
    }
    .media-cover {
      width: 100%;
      max-width: 104px;
      height: 132px;
    }
    .active-card-poster .media-cover {
      width: 100%;
      max-width: none;
      height: 100%;
      min-height: 0;
      border: 0;
      border-radius: 0;
    }
    .completed-cover {
      width: 100%;
      aspect-ratio: 0.72;
      margin-bottom: 14px;
    }
    .media-cover img,
    .completed-cover img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      position: absolute;
      inset: 0;
    }
    .completed-task-list {
      padding: 18px;
      display: grid;
      gap: 12px;
    }
    .completed-list-item {
      padding: 14px 16px;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,0.07);
      background: rgba(255,255,255,0.03);
    }
    .completed-list-item summary {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 14px;
      align-items: center;
      cursor: pointer;
      list-style: none;
    }
    .completed-list-item summary::-webkit-details-marker {
      display: none;
    }
    .completed-list-main {
      display: grid;
      gap: 6px;
      min-width: 0;
    }
    .completed-list-top {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .completed-time-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(132px, 1fr));
      gap: 8px 12px;
      color: var(--text-muted);
      font-size: 12px;
    }
    .completed-time-grid span {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .completed-log {
      margin-top: 12px;
      max-height: 300px;
      overflow: auto;
    }
    .cover-badge {
      position: absolute;
      top: 10px;
      right: 10px;
      font-size: 10px;
      font-weight: 700;
      color: white;
      padding: 5px 8px;
      border-radius: 999px;
      background: rgba(7,16,25,0.72);
      border: 1px solid rgba(255,255,255,0.08);
    }
    .cover-title {
      font-size: 20px;
      font-weight: 800;
      line-height: 1;
      letter-spacing: -0.04em;
      color: white;
      text-shadow: 0 8px 24px rgba(0,0,0,0.42);
    }
    .media-meta {
      display: grid;
      gap: 8px;
      min-width: 0;
    }
    .media-title {
      font-size: 16px;
      font-weight: 700;
      line-height: 1.35;
    }
    .meta-row,
    .meta-stack {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      color: var(--text-muted);
      font-size: 12px;
    }
    .type-chip,
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 9px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.08);
      color: var(--text-strong);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .status-pill.running { color: #ffe7a0; }
    .status-pill.paused { color: #d8c8ff; }
    .status-pill.completed { color: #9bf2bb; }
    .status-pill.failed { color: #ffb5ac; }
    .progress-shell {
      display: grid;
      gap: 8px;
    }
    .progress {
      height: 12px;
      background: rgba(255,255,255,0.06);
    }
    .progress > span {
      box-shadow: 0 0 20px rgba(29,161,255,0.3);
      transition: width 180ms ease;
    }
    .card-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .task-stop-btn,
    .task-pause-btn,
    .secondary-btn {
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.05);
      color: var(--text-strong);
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
    }
    .task-icon-btn {
      width: 30px;
      height: 30px;
      padding: 0;
      border-radius: 999px;
      display: inline-grid;
      place-items: center;
      font-size: 15px;
      line-height: 1;
    }
    .task-stop-btn.task-icon-btn {
      color: #ffcac4;
    }
    a.secondary-btn {
      text-decoration: none;
      display: inline-flex;
      align-items: center;
    }
    .secondary-btn[disabled],
    .task-stop-btn[disabled],
    .task-pause-btn[disabled] {
      opacity: 0.6;
      cursor: default;
    }
    .task-log details {
      border-top: 1px solid rgba(255,255,255,0.06);
      padding-top: 12px;
    }
    .task-log summary {
      cursor: pointer;
      color: var(--text-muted);
      font-size: 12px;
    }
    .log-box {
      margin-top: 10px;
    }
    .task-log details[open] .log-box {
      height: 360px;
      max-height: 42vh;
      overflow: auto;
    }
    .empty-premium {
      padding: 38px 24px;
      text-align: center;
      display: grid;
      gap: 10px;
      justify-items: center;
    }
    .empty-illustration {
      width: 72px;
      height: 72px;
      border-radius: 22px;
      display: grid;
      place-items: center;
      font-size: 28px;
      color: white;
      background: linear-gradient(135deg, rgba(29,161,255,0.28), rgba(139,92,246,0.22));
      border: 1px solid rgba(255,255,255,0.08);
    }
    .empty-illustration svg {
      width: 30px;
      height: 30px;
      fill: none;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .add-download-content {
      padding: 18px;
      display: grid;
      gap: 14px;
    }
    .add-download-form {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
    }
    .minor-copy {
      color: var(--text-muted);
      font-size: 12px;
    }
    .network-panel {
      padding: 18px;
      display: grid;
      gap: 16px;
    }
    .network-value {
      font-size: 28px;
      font-weight: 700;
      letter-spacing: -0.04em;
    }
    .network-chart {
      position: relative;
      height: 148px;
      border-radius: 18px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,0.06);
      background:
        linear-gradient(rgba(255,255,255,0.04) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.04) 1px, transparent 1px),
        linear-gradient(180deg, rgba(10,18,31,0.94), rgba(9,17,28,0.78));
      background-size: 100% 33%, 8.33% 100%, auto;
    }
    .network-chart svg {
      width: 100%;
      height: 100%;
      display: block;
    }
    .network-chart-line.primary {
      stroke: #1d8cff;
      filter: drop-shadow(0 0 10px rgba(29,140,255,0.32));
    }
    .network-chart-line.secondary {
      stroke: #8b5cf6;
      filter: drop-shadow(0 0 10px rgba(139,92,246,0.28));
    }
    .network-chart-fill {
      fill: url(#networkAreaPrimary);
      opacity: 0.32;
    }
    .network-legend {
      display: flex;
      gap: 18px;
      flex-wrap: wrap;
      align-items: center;
      color: var(--text-muted);
      font-size: 12px;
    }
    .network-legend-item {
      display: inline-flex;
      gap: 8px;
      align-items: center;
    }
    .network-legend-swatch {
      width: 10px;
      height: 10px;
      border-radius: 50%;
    }
    .network-legend-swatch.primary {
      background: #1d8cff;
      box-shadow: 0 0 10px rgba(29,140,255,0.4);
    }
    .network-legend-swatch.secondary {
      background: #8b5cf6;
      box-shadow: 0 0 10px rgba(139,92,246,0.4);
    }
    .browser-reconnect-btn {
      min-width: 160px;
    }
    .settings-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    @media (max-width: 1280px) {
      .downloads-page-layout,
      .library-layout,
      .downloads-layout {
        grid-template-columns: 1fr;
      }
    }
    @media (max-width: 1100px) {
      .shell.shell-premium {
        grid-template-columns: minmax(0, 1fr);
      }
      .sidebar {
        position: fixed;
        inset: 0 auto 0 0;
        width: min(320px, calc(100vw - 24px));
        transform: translateX(-110%);
        transition: transform 180ms ease;
        z-index: 35;
        border-radius: 0;
      }
      body.sidebar-open .sidebar {
        transform: translateX(0);
      }
      .mobile-nav-toggle {
        display: inline-flex;
      }
      .app-main {
        margin-left: 0;
        padding-top: 60px;
      }
      .settings-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    @media (max-width: 720px) {
      .downloads-page-layout,
      .library-layout,
      .settings-grid,
      .youtube-lookup-form,
      .media-search-form {
        grid-template-columns: 1fr;
      }
      .quick-actions,
      .music-actions {
        display: grid;
      }
      .download-media-card {
        grid-template-columns: 1fr;
      }
      .active-card-metrics {
        grid-template-columns: 1fr;
        gap: 8px;
      }
      .completed-list-item summary,
      .completed-time-grid {
        grid-template-columns: 1fr;
      }
      .media-modal-body,
      .library-layout.is-download-view .library-browser-stage {
        grid-template-columns: 1fr;
      }
      .auto-request-top {
        grid-template-columns: minmax(0, 1fr) auto;
      }
      .auto-request-time {
        grid-column: 1 / -1;
      }
      .active-card-poster {
        border: 0;
        width: 100%;
        height: 180px;
      }
      .media-card-header {
        grid-template-columns: 64px minmax(0, 1fr);
      }
      .media-cover {
        max-width: 64px;
        height: 92px;
      }
      .active-card-poster .media-cover {
        max-width: none;
        height: 180px;
      }
    }
  </style>
</head>
<body>
  <button class="mobile-nav-toggle" id="mobile-nav-toggle" type="button" aria-expanded="false" aria-controls="sidebar-nav">Menu</button>
  <main class="shell shell-premium">
    <aside class="sidebar" id="sidebar-nav">
      <div class="brand-block">
        <div class="brand-mark">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v18M7 6h5M7 18h10"/></svg>
        </div>
        <div class="brand-meta">
          <h1 class="brand-title">Isambard</h1>
        </div>
      </div>

      <div class="nav-section">
        <nav class="sidebar-nav" aria-label="Primary">
          <a class="nav-item" href="/downloads" aria-selected="__DOWNLOADS_TAB_SELECTED__">
            <span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 20h14"/></svg></span>
            <span>Downloads</span>
          </a>
          <a class="nav-item" href="/movies-tv/discover" aria-selected="__MOVIES_TAB_SELECTED__">
            <span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 3v4M17 3v4M3 10h18"/></svg></span>
            <span>Movies &amp; TV</span>
          </a>
          <a class="nav-item" href="/youtube" aria-selected="__YOUTUBE_TAB_SELECTED__">
            <span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 12s0-3-1-4.5c-.6-1-1.4-1.4-2.4-1.5C16.6 6 12 6 12 6s-4.6 0-6.6.1c-1 .1-1.8.5-2.4 1.5C2 9 2 12 2 12s0 3 1 4.5c.6 1 1.4 1.4 2.4 1.5 2 .1 6.6.1 6.6.1s4.6 0 6.6-.1c1-.1 1.8-.5 2.4-1.5 1-1.5 1-4.5 1-4.5Z"/><path d="M10 9l5 3-5 3V9Z"/></svg></span>
            <span>YouTube</span>
          </a>
          <a class="nav-item" href="/music" aria-selected="__MUSIC_TAB_SELECTED__">
            <span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V6l10-2v12"/><path d="M9 10l10-2"/><circle cx="6" cy="18" r="3"/><circle cx="19" cy="16" r="3"/></svg></span>
            <span>Music</span>
          </a>
          <a class="nav-item" href="/settings" aria-selected="__SETTINGS_TAB_SELECTED__">
            <span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8.5A3.5 3.5 0 1 1 8.5 12 3.5 3.5 0 0 1 12 8.5Z"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.2a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.2a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.2a1.6 1.6 0 0 0 1 1.5h.1a1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.2a1.6 1.6 0 0 0-1.5 1Z"/></svg></span>
            <span>Settings</span>
          </a>
        </nav>
      </div>
      <div class="sidebar-footer">
        <div class="sidebar-footer-bottom">
          <button class="nav-item sidebar-collapse-btn" id="sidebar-collapse-btn" type="button" aria-pressed="false" title="Toggle sidebar">
            <span class="nav-icon">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h4v14H4z"/><path d="M16 7l-4 5 4 5"/></svg>
            </span>
            <span>Collapse</span>
          </button>
        </div>
      </div>
    </aside>

    <div class="app-main">
      <div class="mullvad-banner" id="mullvad-banner">
        <div>
          <div class="mullvad-banner-title">Mullvad Disconnected</div>
          <div class="mullvad-banner-copy">Connect Mullvad to enable browser navigation, stream detection, and outbound media lookups.</div>
        </div>
      </div>
      <section class="__DOWNLOADS_PAGE_CLASS__" data-page="downloads">
      <section class="downloads-page-layout">
        <div class="downloads-main-column">
          <section class="panel premium-panel downloads-tab-only">
            <div class="downloads-search-panel">
                <input id="downloads-page-search" class="search-input" type="search" placeholder="Search downloads…" aria-label="Search downloads">
            </div>
            <div class="downloads-filter-bar">
              <div class="downloads-filter-group" aria-label="Filter downloads by state">
                <span class="downloads-filter-label">State</span>
                <button class="downloads-filter-pill is-active" type="button" data-download-state-filter="all">All</button>
                <button class="downloads-filter-pill" type="button" data-download-state-filter="running">Running</button>
                <button class="downloads-filter-pill" type="button" data-download-state-filter="queued">Queued</button>
                <button class="downloads-filter-pill" type="button" data-download-state-filter="paused">Paused</button>
                <button class="downloads-filter-pill" type="button" data-download-state-filter="completed">Completed</button>
                <button class="downloads-filter-pill" type="button" data-download-state-filter="failed">Failed</button>
                <button class="downloads-filter-pill" type="button" data-download-state-filter="stopped">Stopped</button>
              </div>
              <div class="downloads-filter-group" aria-label="Filter downloads by type">
                <span class="downloads-filter-label">Type</span>
                <button class="downloads-filter-pill is-active" type="button" data-download-type-filter="all">All</button>
                <button class="downloads-filter-pill" type="button" data-download-type-filter="movie">Movies</button>
                <button class="downloads-filter-pill" type="button" data-download-type-filter="tv">TV</button>
                <button class="downloads-filter-pill" type="button" data-download-type-filter="youtube">YouTube</button>
                <button class="downloads-filter-pill" type="button" data-download-type-filter="music">Music</button>
                <button class="downloads-filter-pill" type="button" data-download-type-filter="file">Files</button>
              </div>
            </div>
          </section>
          <div class="downloads-main">
            <section class="panel premium-panel">
              <header class="panel-header premium-header">
                <div>
                  <h3 class="section-title">Active Downloads</h3>
                </div>
                <div class="panel-toolbar">
                  <span class="pill" id="active-count">0 items</span>
                </div>
              </header>
              <div class="download-card-grid" id="active-task-list"></div>
            </section>

            <section class="panel premium-panel">
              <header class="panel-header premium-header">
                <div>
                  <h3 class="section-title">Recently Completed</h3>
                </div>
                <div class="panel-toolbar">
                  <span class="pill" id="completed-count">0 items</span>
                </div>
              </header>
              <div class="completed-task-list" id="completed-task-list"></div>
            </section>
          </div>
        </section>
      </section>

    <section class="__LIBRARY_PAGE_CLASS__" data-page="library">
      <section class="page-hero">
        <div class="page-header-row">
          <div>
            <p class="eyebrow">Movies &amp; TV</p>
            <h2 class="page-title">Movies &amp; TV</h2>
            <p class="page-copy">Discover new releases, queue downloads, and drive YFlix from one place.</p>
          </div>
          <div class="page-header-actions">
            <button class="page-action-btn is-active" type="button" data-media-view="discover">Discover</button>
            <button class="page-action-btn" type="button" data-media-view="search">Search</button>
            <button class="page-action-btn" type="button" data-media-view="download">Download</button>
          </div>
        </div>
        <div class="page-hero-tools library-search-only" id="media-search-tools">
          <form class="media-search-form" id="media-search-form">
            <input class="search-input media-search-input" id="media-search-input" type="search" placeholder="Search movies &amp; TV..." autocomplete="off">
            <button class="youtube-btn primary" type="submit" id="media-search-submit">Search</button>
          </form>
        </div>
      </section>
      <section class="library-layout" id="library-layout">
        <section class="library-content-stage">
          <section class="library-main">
            <section class="panel premium-panel library-highlight-panel">
              <div class="library-highlight" id="library-highlight"></div>
            </section>
            <section class="library-sections" id="media-sections"></section>
          </section>
        </section>
        <section class="library-browser-stage">
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
                  data-browser-src="__BROWSER_URL__"
                  tabindex="0"
                  allow="autoplay; clipboard-read; clipboard-write; fullscreen"
                  referrerpolicy="no-referrer"
                ></iframe>
                <div class="browser-blocked" id="browser-blocked">
                  <div class="browser-blocked-card">
                    <div class="browser-blocked-icon">!</div>
                    <div class="browser-blocked-title">Mullvad Required</div>
                    <div class="browser-blocked-copy" id="browser-blocked-copy">Connect Mullvad to start the remote browser and reach internet destinations.</div>
                    <button class="youtube-btn primary browser-reconnect-btn" type="button" id="browser-reconnect-btn">Reconnect Browser</button>
                  </div>
                </div>
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
          <section class="panel premium-panel auto-download-panel">
            <header class="panel-header premium-header">
              <div>
                <h3 class="section-title">Auto Downloads</h3>
              </div>
            </header>
            <div class="auto-request-list" id="auto-download-request-list"></div>
          </section>
        </section>
      </section>
    </section>

    <section class="__YOUTUBE_PAGE_CLASS__" data-page="youtube">
      <section class="page-hero">
        <div class="page-header-row">
          <div>
            <p class="eyebrow">YouTube</p>
            <h2 class="page-title">YouTube</h2>
            <p class="page-copy">Search and download videos and playlists from YouTube.</p>
          </div>
        </div>
        <div class="page-filter-pills">
          <span class="page-filter-pill is-active">Search</span>
          <span class="page-filter-pill">Subscriptions</span>
          <span class="page-filter-pill">Playlists</span>
          <span class="page-filter-pill">History</span>
        </div>
      </section>
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
      <section class="page-hero">
        <div class="page-header-row">
          <div>
            <p class="eyebrow">Music</p>
            <h2 class="page-title">Music</h2>
            <p class="page-copy">Discover and download music and albums.</p>
          </div>
          <div class="page-header-actions">
            <span class="page-action-btn is-active">Discover</span>
            <span class="page-action-btn">Library</span>
            <span class="page-action-btn">Playlists</span>
          </div>
        </div>
        <div class="page-filter-pills">
          <span class="page-filter-pill is-active">Popular</span>
          <span class="page-filter-pill">New Releases</span>
          <span class="page-filter-pill">Charts</span>
          <span class="page-filter-pill">Genres</span>
          <span class="page-filter-pill">Mood</span>
        </div>
      </section>
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
      <section class="page-hero">
        <div class="page-header-row">
          <div>
            <p class="eyebrow">Settings</p>
            <h2 class="page-title">Settings</h2>
            <p class="page-copy">Configure Isambard to your preferences.</p>
          </div>
        </div>
        <div class="page-filter-pills">
          <span class="page-filter-pill is-active">General</span>
          <span class="page-filter-pill">Downloads</span>
          <span class="page-filter-pill">Integrations</span>
          <span class="page-filter-pill">Advanced</span>
        </div>
      </section>
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
        <section class="panel settings-card">
          <header class="panel-header">
            <div class="panel-title-row">
              <h2 class="panel-title">Storage</h2>
            </div>
          </header>
          <div class="settings-storage">
            <div class="settings-storage-top">
              <div>
                <div class="settings-storage-value" id="sidebar-storage-value">0%</div>
                <div class="settings-summary" id="sidebar-storage-percent">0% used</div>
              </div>
              <div class="settings-summary" id="sidebar-storage-capacity">0 of 0</div>
            </div>
            <div class="storage-progress" aria-hidden="true"><span id="sidebar-storage-bar"></span></div>
            <div class="settings-summary" id="sidebar-storage-detail">Downloads volume</div>
          </div>
        </section>
        <section class="panel settings-card">
          <header class="panel-header">
            <div class="panel-title-row">
              <h2 class="panel-title">Network</h2>
            </div>
          </header>
          <div class="network-panel">
            <div class="network-value" id="network-value">0 Mbps</div>
            <div class="network-chart" id="network-sparkline"></div>
            <div class="network-legend">
              <span class="network-legend-item"><span class="network-legend-swatch primary"></span><span id="network-upload-label">0 Mbps Upload</span></span>
              <span class="network-legend-item"><span class="network-legend-swatch secondary"></span><span id="network-download-label">0 Mbps Download</span></span>
            </div>
            <div class="minor-copy" id="network-pill">0 Mbps</div>
          </div>
        </section>
        <section class="panel settings-card empty-card">
          <header class="panel-header">
            <div class="panel-title-row">
              <h2 class="panel-title">Movies &amp; TV</h2>
            </div>
          </header>
          <div class="settings-summary" id="jellyfin-settings-summary">Jellyfin link not configured.</div>
          <div class="settings-summary" id="tmdb-settings-summary">TMDb integration not configured.</div>
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
    </div>
  </main>

  <div class="media-modal" id="media-modal" aria-hidden="true">
    <div class="media-modal-card">
      <div class="media-modal-hero" id="media-modal-hero">
        <button class="media-modal-close" type="button" id="media-modal-close" aria-label="Close">×</button>
      </div>
      <div class="media-modal-body">
        <div class="media-modal-poster" id="media-modal-poster"></div>
        <div class="media-modal-copy">
          <div class="media-modal-meta" id="media-modal-meta"></div>
          <h3 class="media-modal-title" id="media-modal-title">Title</h3>
          <p class="media-modal-overview" id="media-modal-overview"></p>
          <div class="media-modal-actions" id="media-modal-actions"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="auto-download-toasts" id="auto-download-toasts" aria-live="polite" aria-atomic="false"></div>

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
    const AUTO_DOWNLOAD_REQUESTS_KEY = "isambard.autoDownloadRequests";
    const AUTO_DOWNLOAD_TOASTS_KEY = "isambard.autoDownloadToasts";
    const AUTO_DOWNLOAD_TOAST_DURATION = 5000;
    window.__discoverState = __INITIAL_DISCOVER_STATE__;
    window.__mediaLoaded = !!(window.__discoverState?.sections || []).length;

    function localBrowserEmbedUrl(value) {
      try {
        const url = new URL(value, window.location.href);
        const isLocalhost = url.hostname === "localhost" || url.hostname === "127.0.0.1";
        const currentIsLocalhost = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
        if (isLocalhost && !currentIsLocalhost) {
          url.hostname = window.location.hostname;
          url.protocol = window.location.protocol;
        }
        return url.toString();
      } catch (_error) {
        return value;
      }
    }

    if (browserFrame) {
      const browserSrc = localBrowserEmbedUrl(browserFrame.dataset.browserSrc || browserFrame.src || "");
      browserFrame.dataset.browserSrc = browserSrc;
      if (browserFrame.src !== browserSrc) {
        browserFrame.src = browserSrc;
      }
    }

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
      const mullvadConnected = !!window.__systemSummary?.mullvad?.connected;
      const pageChanged = !!state.page_url && state.page_url !== lastBrowserPageUrl;
      const siteChanged = !!state.site && state.site !== lastBrowserSite;
      if (backButton) {
        backButton.disabled = !mullvadConnected || !state.can_go_back;
      }
      if (forwardButton) {
        forwardButton.disabled = !mullvadConnected || !state.can_go_forward;
      }
      if (siteSelect) {
        const effectiveSite = pendingBrowserSite || state.site || "yflix";
        siteSelect.value = effectiveSite;
        siteSelect.disabled = !mullvadConnected;
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

    function downloadsTaskUrl(taskId) {
      const id = String(taskId || "").trim();
      return id ? `/downloads?task=${encodeURIComponent(id)}` : "/downloads";
    }

    function highlightedTaskIdFromLocation() {
      if (window.location.pathname !== "/downloads") return "";
      return new URLSearchParams(window.location.search).get("task") || "";
    }

    function taskStatusLabel(task) {
      const status = String(task?.status || "").toLowerCase();
      if (status === "running") return `Downloading ${Math.round(task.progress || 0)}%`;
      if (status === "queued") return "Queued";
      if (status === "paused") return "Paused";
      if (status === "completed") return "Completed";
      if (status === "failed") return "Failed";
      if (status === "stopped") return "Stopped";
      return "Download";
    }

    function autoFindItemsForPayload(payload) {
      const batchItems = Array.isArray(payload?.batch_items) ? payload.batch_items.filter(Boolean) : [];
      return batchItems.length ? batchItems : [payload];
    }

    function activeMediaTaskForPayload(payload) {
      const payloads = autoFindItemsForPayload(payload);
      return allTasks().find((task) => {
        const status = String(task?.status || "").toLowerCase();
        return ["queued", "running", "paused"].includes(status) && payloads.some((item) => taskMatchesAutoFind(task, item));
      }) || null;
    }

    function activeAutoDownloadRequestForPayload(payload) {
      const payloads = autoFindItemsForPayload(payload);
      return restoreAutoDownloadRequests().find((request) => {
        const status = String(request?.status || "").toLowerCase();
        if (["completed", "failed", "stopped"].includes(status)) return false;
        const requestTask = { ...(request.payload || {}), status: "queued" };
        return payloads.some((item) => taskMatchesAutoFind(requestTask, item));
      }) || null;
    }

    function taskSourceLabel(task) {
      const source = taskSourceKey(task);
      if (source === "tv") return "TV";
      if (source === "movie") return "Movie";
      if (source === "youtube") return "YouTube";
      if (source === "music") return "Music";
      return "File";
    }

    function taskSourceKey(task) {
      const source = String(task?.source_type || task?.media_type || "file").toLowerCase();
      if (["tv", "show", "series"].includes(source)) return "tv";
      if (["movie", "film"].includes(source)) return "movie";
      if (source.includes("youtube")) return "youtube";
      if (source.includes("music") || source.includes("audio")) return "music";
      return "file";
    }

    function groupedTasks() {
      const search = String(window.__downloadSearch || "").trim().toLowerCase();
      const stateFilter = String(window.__downloadStateFilter || "all").toLowerCase();
      const typeFilter = String(window.__downloadTypeFilter || "all").toLowerCase();
      const matches = (task) => {
        const matchesSearch = !search || [task.title, task.url, task.output_template, task.media_type, task.source_type]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(search));
        const matchesState = stateFilter === "all" || String(task.status || "").toLowerCase() === stateFilter;
        const matchesType = typeFilter === "all" || taskSourceKey(task) === typeFilter;
        return matchesSearch && matchesState && matchesType;
      };
      const tasks = allTasks().filter(matches);
      const active = tasks
        .filter((task) => task.status === "running" || task.status === "queued" || task.status === "paused")
        .sort((a, b) => {
          const aPriority = a.status === "running" ? 0 : a.status === "queued" ? 1 : 2;
          const bPriority = b.status === "running" ? 0 : b.status === "queued" ? 1 : 2;
          if (aPriority !== bPriority) return aPriority - bPriority;
          return (parseTime(b.started_at || b.created_at)?.getTime() || 0) - (parseTime(a.started_at || a.created_at)?.getTime() || 0);
        });
      const completed = tasks
        .filter((task) => ["completed", "failed", "stopped"].includes(task.status))
        .sort((a, b) => (parseTime(b.finished_at)?.getTime() || 0) - (parseTime(a.finished_at)?.getTime() || 0));
      const activity = tasks
        .filter((task) => ["queued", "running", "paused", "completed", "failed", "stopped"].includes(task.status))
        .sort((a, b) => {
          const aTime = parseTime(a.finished_at || a.started_at || a.created_at)?.getTime() || 0;
          const bTime = parseTime(b.finished_at || b.started_at || b.created_at)?.getTime() || 0;
          return bTime - aTime;
        });
      return { active, completed, activity };
    }

    function parseSpeedMbps(value) {
      const text = String(value || "").trim();
      const match = text.match(/([0-9]+(?:\\.[0-9]+)?)\\s*([KMGTP]?i?B)\\/s/i);
      if (!match) return 0;
      const amount = Number(match[1]);
      const unit = match[2].toUpperCase();
      const multipliers = {
        B: 1,
        KB: 1e3,
        KIB: 1024,
        MB: 1e6,
        MIB: 1024 ** 2,
        GB: 1e9,
        GIB: 1024 ** 3,
        TB: 1e12,
        TIB: 1024 ** 4
      };
      const bytesPerSecond = amount * (multipliers[unit] || 1);
      return (bytesPerSecond * 8) / 1e6;
    }

    function formatMbps(value) {
      if (!Number.isFinite(value) || value <= 0) return "0 Mbps";
      if (value >= 100) return `${Math.round(value)} Mbps`;
      if (value >= 10) return `${value.toFixed(1)} Mbps`;
      return `${value.toFixed(2)} Mbps`;
    }

    function formatBytes(bytes) {
      const value = Number(bytes || 0);
      if (!Number.isFinite(value) || value <= 0) return "0 B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let unitIndex = 0;
      let size = value;
      while (size >= 1024 && unitIndex < units.length - 1) {
        size /= 1024;
        unitIndex += 1;
      }
      return `${size >= 10 || unitIndex === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unitIndex]}`;
    }

    function formatRelativeTime(value) {
      const parsed = parseTime(value);
      if (!parsed) return "Unknown time";
      const diffMs = Date.now() - parsed.getTime();
      const diffMinutes = Math.round(diffMs / 60000);
      if (Math.abs(diffMinutes) < 1) return "Just now";
      if (Math.abs(diffMinutes) < 60) return `${Math.abs(diffMinutes)}m ${diffMinutes >= 0 ? "ago" : "from now"}`;
      const diffHours = Math.round(diffMinutes / 60);
      if (Math.abs(diffHours) < 24) return `${Math.abs(diffHours)}h ${diffHours >= 0 ? "ago" : "from now"}`;
      const diffDays = Math.round(diffHours / 24);
      return `${Math.abs(diffDays)}d ${diffDays >= 0 ? "ago" : "from now"}`;
    }

    function formatDateTime(value) {
      const parsed = parseTime(value);
      if (!parsed) return "Unknown";
      return new Intl.DateTimeFormat(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit"
      }).format(parsed);
    }

    function formatDurationBetween(startValue, endValue) {
      const start = parseTime(startValue);
      const end = parseTime(endValue);
      if (!start || !end) return "Unknown";
      const totalSeconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000));
      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const seconds = totalSeconds % 60;
      if (hours) return `${hours}h ${minutes}m`;
      if (minutes) return `${minutes}m ${seconds}s`;
      return `${seconds}s`;
    }

    function normalizeArtworkKey(value) {
      return cleanMediaTitle(value || "")
        .toLowerCase()
        .replace(/\\(\\s*(?:19|20)\\d{2}\\s*\\)/g, " ")
        .replace(/\\b(?:19|20)\\d{2}\\b/g, " ")
        .replace(/[^a-z0-9]+/g, " ")
        .trim();
    }

    function artworkKeysForTitle(value) {
      const raw = cleanMediaTitle(value || "");
      const keys = [
        normalizeArtworkKey(raw),
        normalizeArtworkKey(raw.replace(/\\(\\s*(?:19|20)\\d{2}\\s*\\)/g, "")),
        normalizeArtworkKey(raw.replace(/\\b(?:19|20)\\d{2}\\b/g, "")),
      ].filter(Boolean);
      return Array.from(new Set(keys));
    }

    function artworkSearchTitle(value) {
      return cleanMediaTitle(value || "")
        .replace(/\\(\\s*(?:19|20)\\d{2}\\s*\\)/g, " ")
        .replace(/\\b(?:19|20)\\d{2}\\b/g, " ")
        .replace(/\\s+/g, " ")
        .trim();
    }

    function rememberArtworkItem(item) {
      if (!item?.poster_url) return false;
      window.__mediaArtworkByTitle = window.__mediaArtworkByTitle || {};
      let changed = false;
      artworkKeysForTitle(item.title || "").forEach((key) => {
        if (key && !window.__mediaArtworkByTitle[key]) {
          window.__mediaArtworkByTitle[key] = item;
          changed = true;
        }
      });
      return changed;
    }

    function taskArtwork(task) {
      if (task?.poster_url) {
        return {
          title: task.series_name || task.title || "Poster",
          poster_url: task.poster_url,
          backdrop_url: task.backdrop_url || "",
        };
      }
      const map = window.__mediaArtworkByTitle || {};
      const keys = [
        ...artworkKeysForTitle(task?.title || ""),
        ...artworkKeysForTitle(task?.series_name || ""),
      ];
      for (const key of keys) {
        const match = map[key];
        if (match?.poster_url) {
          return match;
        }
      }
      return null;
    }

    function hydrateTaskArtwork(tasks) {
      window.__artworkLookupDone = window.__artworkLookupDone || {};
      window.__artworkLookupInFlight = window.__artworkLookupInFlight || {};
      (tasks || []).forEach((task) => {
        if (taskArtwork(task)) return;
        const query = artworkSearchTitle(task.series_name || task.title || "");
        const key = normalizeArtworkKey(query);
        if (!query || !key || window.__artworkLookupDone[key] || window.__artworkLookupInFlight[key]) return;
        window.__artworkLookupInFlight[key] = true;
        fetch(`/api/media/discover?query=${encodeURIComponent(query)}`)
          .then((response) => response.ok ? response.json() : null)
          .then((state) => {
            const items = (state?.sections || []).flatMap((section) => section.items || []).concat(state?.items || []);
            const changed = items.some((item) => rememberArtworkItem(item));
            window.__artworkLookupDone[key] = true;
            if (changed) renderTasks();
          })
          .catch(() => {
            window.__artworkLookupDone[key] = true;
          })
          .finally(() => {
            delete window.__artworkLookupInFlight[key];
          });
      });
    }

    function buildCover(task) {
      const artwork = taskArtwork(task);
      if (artwork?.poster_url) {
        return `<img src="${escapeHtml(artwork.poster_url)}" alt="${escapeHtml(artwork.title || task.title || "Poster")}">`;
      }
      const label = taskSourceLabel(task);
      const title = cleanMediaTitle(task.title || "Untitled") || "Untitled";
      const initials = title
        .split(/\\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map((part) => part[0]?.toUpperCase() || "")
        .join("") || label[0];
      return `
        <div class="cover-badge">${escapeHtml(label)}</div>
        <div class="cover-title">${escapeHtml(initials)}</div>
      `;
    }

    function progressMeta(task) {
      const parts = [];
      if (task.filesize) parts.push(task.filesize);
      if (task.speed) parts.push(task.speed);
      if (task.eta) parts.push(`ETA ${task.eta}`);
      return parts.join(" • ") || "Waiting for metadata";
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

    function findTaskForStream(url) {
      return allTasks().find((task) => task.url === url) || null;
    }

    function captureTaskUiState(scope) {
      const state = {};
      scope?.querySelectorAll(".download-media-card[data-task-id]").forEach((card) => {
        const taskId = card.dataset.taskId;
        if (!taskId) return;
        const details = card.querySelector(".task-log details");
        const logBox = card.querySelector(".log-box");
        state[taskId] = {
          logsOpen: !!details?.open,
          logScrollTop: logBox?.scrollTop || 0,
        };
      });
      return state;
    }

    function restoreTaskUiState(scope, state) {
      scope?.querySelectorAll(".download-media-card[data-task-id]").forEach((card) => {
        const taskId = card.dataset.taskId;
        const saved = taskId ? state[taskId] : null;
        if (!saved) return;
        const details = card.querySelector(".task-log details");
        const logBox = card.querySelector(".log-box");
        if (details) details.open = !!saved.logsOpen;
        if (logBox) logBox.scrollTop = saved.logScrollTop || 0;
      });
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

    function activeTaskCard(task, savedState = {}) {
      const progress = task.status === "completed" ? 100 : Math.max(0, Math.min(100, task.progress || 0));
      const logs = task.output || task.error || "No logs yet.";
      const startedLabel = task.status === "queued"
        ? `Queued ${formatRelativeTime(task.created_at)}`
        : `Started ${formatRelativeTime(task.started_at || task.created_at)}`;
      const outputPath = String(task.output_template || "").replace(/%\\((ext)\\)s/g, "mp4");
      const pauseAction = task.status === "paused" ? "resume" : "pause";
      const pauseLabel = task.status === "paused" ? "Resume" : "Pause";
      const pauseIcon = task.status === "paused" ? "▶" : "Ⅱ";
      return `
        <article class="download-media-card" data-task-id="${escapeHtml(task.id)}">
          <div class="active-card-poster" data-role="poster">
            <div class="media-cover">${buildCover(task)}</div>
          </div>
          <div class="active-card-main">
            <div class="active-card-top">
              <div class="active-card-body">
                <div class="meta-row">
                  <span class="type-chip" data-role="type">${escapeHtml(taskSourceLabel(task))}</span>
                  <span class="status-pill ${escapeHtml(task.status)}" data-role="status">${escapeHtml(task.status)}</span>
                </div>
                <div class="media-title" data-role="title">${escapeHtml(cleanMediaTitle(task.title || "Untitled"))}</div>
                <div class="meta-stack">
                  <span data-role="started">${escapeHtml(startedLabel)}</span>
                  <span data-role="output-path">${escapeHtml(outputPath || "")}</span>
                </div>
              </div>
              <div class="active-card-progress-tools">
                <strong data-role="progress-label">${escapeHtml(String(Math.round(progress)))}%</strong>
                <button class="task-pause-btn task-icon-btn" type="button" data-task-control="pause" data-task-id="${escapeHtml(task.id)}" data-task-action="${escapeHtml(pauseAction)}" data-role="pause" aria-label="${escapeHtml(pauseLabel)}" title="${escapeHtml(pauseLabel)}">${pauseIcon}</button>
                <button class="task-stop-btn task-icon-btn" type="button" data-stop-task-id="${escapeHtml(task.id)}" data-role="stop" aria-label="${task.status === "queued" ? "Remove" : "Cancel"}" title="${task.status === "queued" ? "Remove" : "Cancel"}">×</button>
              </div>
            </div>
            <div class="progress-shell">
              <div class="progress ${escapeHtml(task.status)}" data-role="progress"><span style="width:${progress}%"></span></div>
            </div>
            <div class="active-card-metrics">
              <span data-role="speed">${escapeHtml(task.speed || "Pending speed")}</span>
              <span data-role="filesize">${escapeHtml(task.filesize || "Waiting for size")}</span>
              <span data-role="eta">${escapeHtml(task.eta ? `ETA ${task.eta}` : "ETA pending")}</span>
            </div>
            <div class="task-log">
              <details${savedState.logsOpen ? " open" : ""}>
                <summary>View logs</summary>
                <pre class="log-box" data-role="logs">${escapeHtml(logs)}</pre>
              </details>
            </div>
          </div>
        </article>
      `;
    }

    function setTextIfChanged(node, value) {
      if (node && node.textContent !== value) {
        node.textContent = value;
      }
    }

    function updateActiveTaskCard(card, task) {
      const progress = task.status === "completed" ? 100 : Math.max(0, Math.min(100, task.progress || 0));
      const logs = task.output || task.error || "No logs yet.";
      const startedLabel = task.status === "queued"
        ? `Queued ${formatRelativeTime(task.created_at)}`
        : `Started ${formatRelativeTime(task.started_at || task.created_at)}`;
      const outputPath = String(task.output_template || "").replace(/%\\((ext)\\)s/g, "mp4");
      setTextIfChanged(card.querySelector('[data-role="type"]'), taskSourceLabel(task));
      const status = card.querySelector('[data-role="status"]');
      if (status) {
        status.className = `status-pill ${task.status || ""}`;
        setTextIfChanged(status, task.status || "");
      }
      setTextIfChanged(card.querySelector('[data-role="title"]'), cleanMediaTitle(task.title || "Untitled"));
      setTextIfChanged(card.querySelector('[data-role="started"]'), startedLabel);
      setTextIfChanged(card.querySelector('[data-role="output-path"]'), outputPath || "");
      setTextIfChanged(card.querySelector('[data-role="progress-label"]'), `${Math.round(progress)}%`);
      const progressEl = card.querySelector('[data-role="progress"]');
      if (progressEl) {
        progressEl.className = `progress ${task.status || ""}`;
        const bar = progressEl.querySelector("span");
        if (bar) bar.style.width = `${progress}%`;
      }
      setTextIfChanged(card.querySelector('[data-role="speed"]'), task.speed || "Pending speed");
      setTextIfChanged(card.querySelector('[data-role="filesize"]'), task.filesize || "Waiting for size");
      setTextIfChanged(card.querySelector('[data-role="eta"]'), task.eta ? `ETA ${task.eta}` : "ETA pending");
      setTextIfChanged(card.querySelector('[data-role="logs"]'), logs);
      const pauseButton = card.querySelector('[data-role="pause"]');
      if (pauseButton) {
        const pauseAction = task.status === "paused" ? "resume" : "pause";
        const pauseLabel = task.status === "paused" ? "Resume" : "Pause";
        pauseButton.dataset.taskAction = pauseAction;
        pauseButton.title = pauseLabel;
        pauseButton.setAttribute("aria-label", pauseLabel);
        setTextIfChanged(pauseButton, task.status === "paused" ? "▶" : "Ⅱ");
      }
      const stopButton = card.querySelector('[data-role="stop"]');
      if (stopButton) {
        const stopLabel = task.status === "queued" ? "Remove" : "Cancel";
        stopButton.title = stopLabel;
        stopButton.setAttribute("aria-label", stopLabel);
      }
      const coverHtml = buildCover(task);
      const poster = card.querySelector('[data-role="poster"] .media-cover');
      if (poster && poster.innerHTML !== coverHtml) {
        poster.innerHTML = coverHtml;
      }
    }

    function renderActiveTasks(activeList, tasks, activeUiState) {
      const existing = new Map(Array.from(activeList.querySelectorAll(".download-media-card[data-task-id]")).map((card) => [card.dataset.taskId, card]));
      tasks.forEach((task) => {
        let card = existing.get(task.id);
        if (!card) {
          const template = document.createElement("template");
          template.innerHTML = activeTaskCard(task, activeUiState[task.id] || {}).trim();
          card = template.content.firstElementChild;
          activeList.appendChild(card);
          bindTaskButtons(card);
        } else {
          updateActiveTaskCard(card, task);
        }
        existing.delete(task.id);
      });
      existing.forEach((card) => card.remove());
      const orderedCards = tasks
        .map((task) => activeList.querySelector(`.download-media-card[data-task-id="${CSS.escape(task.id)}"]`))
        .filter(Boolean);
      orderedCards.forEach((card, index) => {
        const current = activeList.children[index] || null;
        if (current !== card) {
          activeList.insertBefore(card, current);
        }
      });
    }

    function captureCompletedUiState(scope) {
      const state = {};
      scope?.querySelectorAll(".completed-list-item[data-task-id]").forEach((row) => {
        const taskId = row.dataset.taskId;
        if (!taskId) return;
        state[taskId] = {
          open: !!row.open,
          logScrollTop: row.querySelector(".completed-log")?.scrollTop || 0
        };
      });
      return state;
    }

    function restoreCompletedUiState(scope, state) {
      scope?.querySelectorAll(".completed-list-item[data-task-id]").forEach((row) => {
        const saved = state[row.dataset.taskId];
        if (!saved) return;
        row.open = !!saved.open;
        const log = row.querySelector(".completed-log");
        if (log) log.scrollTop = saved.logScrollTop || 0;
      });
    }

    function completedTaskCard(task) {
      const finished = task.finished_at ? formatRelativeTime(task.finished_at) : "Unknown time";
      const logs = task.output || task.error || "No logs available.";
      const artwork = taskArtwork(task);
      const jellyfinUrl = artwork?.jellyfin_url || jellyfinSearchUrl(task.series_name || task.title || "");
      const status = task.status || "completed";
      const statusLabel = status.charAt(0).toUpperCase() + status.slice(1);
      return `
        <details class="completed-list-item" data-task-id="${escapeHtml(task.id)}">
          <summary>
            <div class="completed-list-main">
              <div class="completed-list-top">
                <span class="type-chip">${escapeHtml(taskSourceLabel(task))}</span>
                <div class="media-title">${escapeHtml(cleanMediaTitle(task.title || "Untitled"))}</div>
              </div>
            </div>
            <div class="card-actions">
              <span class="meta-row">${escapeHtml(finished)}</span>
              <span class="status-pill ${escapeHtml(status)}">${escapeHtml(statusLabel)}</span>
              ${jellyfinUrl ? `<a class="secondary-btn" href="${escapeHtml(jellyfinUrl)}" target="_blank" rel="noreferrer">Open Jellyfin</a>` : ""}
            </div>
          </summary>
          <div class="completed-time-grid">
            <span>Queued ${escapeHtml(formatDateTime(task.created_at))}</span>
            <span>Started ${escapeHtml(formatDateTime(task.started_at))}</span>
            <span>Completed ${escapeHtml(formatDateTime(task.finished_at))}</span>
            <span>Took ${escapeHtml(formatDurationBetween(task.started_at || task.created_at, task.finished_at))}</span>
          </div>
          <pre class="log-box completed-log">${escapeHtml(logs)}</pre>
        </details>
      `;
    }

    function bindTaskButtons(scope) {
      scope.querySelectorAll(".task-pause-btn[data-task-id]").forEach((button) => {
        button.addEventListener("click", async () => {
          const taskId = button.dataset.taskId;
          const action = button.dataset.taskAction === "resume" ? "resume" : "pause";
          if (!taskId) return;
          button.disabled = true;
          try {
            await fetch(`/api/tasks/${taskId}/${action}`, { method: "POST" });
            await refresh();
          } finally {
            button.disabled = false;
          }
        });
      });
      scope.querySelectorAll(".task-stop-btn[data-stop-task-id]").forEach((button) => {
        button.addEventListener("click", async () => {
          const taskId = button.dataset.stopTaskId;
          if (!taskId) return;
          button.disabled = true;
          try {
            await fetch(`/api/tasks/${taskId}/stop`, { method: "POST" });
            await refresh();
          } finally {
            button.disabled = false;
          }
        });
      });
    }

    function applyHighlightedDownloadTask() {
      const taskId = highlightedTaskIdFromLocation();
      document.querySelectorAll("[data-task-id].is-highlighted").forEach((node) => {
        node.classList.remove("is-highlighted");
      });
      if (!taskId) return;
      const target = document.querySelector(`[data-task-id="${CSS.escape(taskId)}"]`);
      if (!target) return;
      target.classList.add("is-highlighted");
      if (!window.__lastHighlightedTaskScroll || window.__lastHighlightedTaskScroll !== taskId) {
        window.__lastHighlightedTaskScroll = taskId;
        target.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }

    function renderStats(groups) {
      const activeCount = groups.active.length;
      const throughput = groups.active.reduce((sum, task) => sum + parseSpeedMbps(task.speed), 0);
      document.getElementById("network-value").textContent = formatMbps(throughput);
      document.getElementById("network-pill").textContent = formatMbps(throughput);

      window.__networkHistory = [...(window.__networkHistory || []), throughput].slice(-24);
      window.__uploadHistory = [...(window.__uploadHistory || []), Math.max(0, throughput * 0.18 + (activeCount ? 2 : 0))].slice(-24);
    }

    function renderNetworkSparkline() {
      const downloadValues = window.__networkHistory || [];
      const uploadValues = window.__uploadHistory || [];
      const container = document.getElementById("network-sparkline");
      if (!container) return;
      if (!downloadValues.length) {
        container.innerHTML = `<div class="minor-copy">No network samples yet.</div>`;
        return;
      }
      const width = 320;
      const height = 148;
      const max = Math.max(...downloadValues, ...uploadValues, 1);
      const toPoints = (values, graphHeight, invertOffset = 0) => values.map((value, index) => {
        const x = (index / Math.max(values.length - 1, 1)) * width;
        const y = height - invertOffset - ((value / max) * graphHeight + 12);
        return `${x.toFixed(2)},${Math.max(10, Math.min(height - 10, y)).toFixed(2)}`;
      }).join(" ");
      const primaryPoints = toPoints(downloadValues, 96);
      const secondaryPoints = toPoints(uploadValues, 72, 8);
      const areaPoints = `0,${height} ${primaryPoints} ${width},${height}`;
      container.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <linearGradient id="networkAreaPrimary" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="rgba(29,140,255,0.48)"></stop>
              <stop offset="100%" stop-color="rgba(29,140,255,0)"></stop>
            </linearGradient>
          </defs>
          <polygon class="network-chart-fill" points="${areaPoints}"></polygon>
          <polyline class="network-chart-line primary" points="${primaryPoints}" fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></polyline>
          <polyline class="network-chart-line secondary" points="${secondaryPoints}" fill="none" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"></polyline>
        </svg>
      `;
      const latestDownload = downloadValues[downloadValues.length - 1] || 0;
      const latestUpload = uploadValues[uploadValues.length - 1] || 0;
      const uploadLabel = document.getElementById("network-upload-label");
      const downloadLabel = document.getElementById("network-download-label");
      if (uploadLabel) uploadLabel.textContent = `${formatMbps(latestUpload)} Upload`;
      if (downloadLabel) downloadLabel.textContent = `${formatMbps(latestDownload)} Download`;
    }

    function renderTasks() {
      const groups = groupedTasks();
      const activeList = document.getElementById("active-task-list");
      const completedList = document.getElementById("completed-task-list");
      const activeUiState = captureTaskUiState(activeList);
      const completedUiState = captureCompletedUiState(completedList);
      document.getElementById("active-count").textContent = `${groups.active.length} items`;
      document.getElementById("completed-count").textContent = `${groups.completed.length} items`;

      if (!groups.active.length) {
        activeList.innerHTML = `
          <div class="empty-premium">
            <div class="empty-illustration">${downloadIconSvg()}</div>
            <strong>No active downloads</strong>
            <div class="minor-copy">Add a magnet link, torrent, URL, or start a browser capture.</div>
          </div>
        `;
      } else {
        const emptyState = activeList.querySelector(".empty-premium");
        if (emptyState) activeList.innerHTML = "";
        renderActiveTasks(activeList, groups.active, activeUiState);
        restoreTaskUiState(activeList, activeUiState);
        hydrateTaskArtwork(groups.active);
      }

      if (!groups.completed.length) {
        completedList.innerHTML = `<div class="empty">No completed media yet.</div>`;
      } else {
        completedList.innerHTML = groups.completed.slice(0, 12).map(completedTaskCard).join("");
        restoreCompletedUiState(completedList, completedUiState);
      }

      renderStats(groups);
      renderNetworkSparkline();
      applyHighlightedDownloadTask();
      renderOpenMediaModalActions();
    }

    function notifyTaskStatusChanges() {
      const previous = window.__knownTaskStatuses;
      const next = {};
      allTasks().forEach((task) => {
        if (!task?.id) return;
        const status = String(task.status || "");
        next[task.id] = status;
        if (!previous) return;
        const oldStatus = previous[task.id];
        if (status === "completed" && oldStatus !== "completed") {
          showAutoDownloadToast("Download complete", cleanMediaTitle(task.title || "Download finished"), {
            id: `download-complete-${task.id}`,
            href: downloadsTaskUrl(task.id),
          });
        }
      });
      window.__knownTaskStatuses = next;
    }

    function activePageName() {
      return document.querySelector(".page.is-active")?.dataset.page || "downloads";
    }

    function currentMediaView() {
      return window.__mediaView || "discover";
    }

    function updatePageVisibilityState() {
      const page = activePageName();
      document.querySelectorAll(".downloads-tab-only").forEach((node) => {
        node.style.display = page === "downloads" ? "" : "none";
      });
    }

    function setMediaArtworkMap() {
      const artwork = {};
      const states = [window.__discoverState, window.__searchState];
      states.forEach((state) => {
        const sections = state?.sections || [];
        sections.forEach((section) => {
          (section.items || []).forEach((item) => {
            const key = normalizeArtworkKey(item.title || "");
            const keys = artworkKeysForTitle(item.title || "");
            keys.forEach((itemKey) => {
              if (itemKey && item.poster_url && !artwork[itemKey]) {
                artwork[itemKey] = item;
              }
            });
            if (key && item.poster_url && !artwork[key]) {
              artwork[key] = item;
            }
          });
        });
        (state?.items || []).forEach((item) => {
          const key = normalizeArtworkKey(item.title || "");
          const keys = artworkKeysForTitle(item.title || "");
          keys.forEach((itemKey) => {
            if (itemKey && item.poster_url && !artwork[itemKey]) {
              artwork[itemKey] = item;
            }
          });
        });
      });
      window.__mediaArtworkByTitle = artwork;
    }

    function mediaPathForView(view) {
      if (view === "download") return "/movies-tv/download";
      if (view === "search") return "/movies-tv/search";
      return "/movies-tv/discover";
    }

    function normalizeMediaView(name) {
      if (name === "download" || name === "search") return name;
      return "discover";
    }

    function jellyfinSearchUrl(title) {
      const base = window.__systemSummary?.jellyfin?.url || "";
      if (!base) return "";
      const cleanBase = base.endsWith("/") ? base.slice(0, -1) : base;
      const query = String(title || "").trim();
      if (!query) return `${cleanBase}/web/`;
      return `${cleanBase}/web/#/search.html?query=${encodeURIComponent(query)}`;
    }

    async function fetchLocalMediaStatuses(requests) {
      const results = await Promise.all((requests || []).map(async (request) => {
        try {
          const response = await fetch("/api/media/local-status", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(request.payload)
          });
          const status = response.ok ? await response.json() : { exists: false };
          return [request.key, status];
        } catch (_error) {
          return [request.key, { exists: false }];
        }
      }));
      return Object.fromEntries(results);
    }

    function heroCarouselItems() {
      const sections = window.__discoverState?.sections || [];
      return sections
        .filter((section) => section.id === "trending_movies" || section.id === "trending_tv" || section.id === "popular_movies")
        .flatMap((section) => section.items || [])
        .slice(0, 6);
    }

    function setHeroCarouselIndex(nextIndex) {
      const items = heroCarouselItems();
      if (!items.length) {
        window.__heroCarouselIndex = 0;
        return;
      }
      const total = items.length;
      window.__heroCarouselIndex = ((nextIndex % total) + total) % total;
      renderMediaHighlight();
    }

    function switchMediaView(name, options = {}) {
      const { updateHistory = true, historyMode = "replace" } = options;
      window.__mediaView = normalizeMediaView(name);
      document.querySelectorAll("[data-media-view]").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.mediaView === window.__mediaView);
      });
      const layout = document.getElementById("library-layout");
      const searchTools = document.getElementById("media-search-tools");
      if (layout) {
        layout.classList.toggle("is-download-view", window.__mediaView === "download");
      }
      if (searchTools) {
        searchTools.style.display = window.__mediaView === "download" ? "none" : "grid";
      }
      if (window.__mediaView === "download" && window.__systemSummary?.mullvad?.connected) {
        renderBrowserAvailability(true);
      }
      if (updateHistory && window.location.pathname !== mediaPathForView(window.__mediaView)) {
        const method = historyMode === "push" ? "pushState" : "replaceState";
        history[method]({}, "", mediaPathForView(window.__mediaView));
      }
      renderAutoDownloadRequests();
      renderMediaSections();
    }

    function renderMediaHighlight() {
      const root = document.getElementById("library-highlight");
      if (!root) return;
      if (currentMediaView() !== "discover") {
        root.parentElement?.style.setProperty("display", "none");
        root.innerHTML = "";
        root.style.backgroundImage = "";
        return;
      }
      const state = window.__discoverState;
      root.parentElement?.style.setProperty("display", "");
      const highlightItems = heroCarouselItems();
      const item = highlightItems[Math.abs(window.__heroCarouselIndex || 0) % Math.max(highlightItems.length, 1)];
      if (!item) {
        root.innerHTML = `
          <div class="library-highlight-label">Featured</div>
          <h3 class="library-highlight-title">No media loaded</h3>
          <p class="library-highlight-copy">Connect Mullvad and configure TMDb to populate this page.</p>
        `;
        root.style.backgroundImage = "";
        return;
      }
      const backdrop = item.backdrop_url ? `linear-gradient(180deg, rgba(7,16,26,0.08), rgba(7,16,26,0.92)), url("${item.backdrop_url}")` : "";
      root.style.backgroundImage = backdrop || "";
      root.style.backgroundSize = backdrop ? "cover" : "";
      root.style.backgroundPosition = backdrop ? "center" : "";
      const modalPayload = encodeURIComponent(JSON.stringify(item));
      root.innerHTML = `
        <div class="library-highlight-stage" id="library-highlight-stage" data-media-modal="${escapeHtml(modalPayload)}">
          <div class="library-highlight-copy-group">
            <h3 class="library-highlight-title">${escapeHtml(item.title || "Untitled")}</h3>
            <p class="library-highlight-copy">${escapeHtml(item.overview || "Poster-backed media discovery and download handoff live here.")}</p>
          </div>
          <div class="library-highlight-meta">
            ${item.year ? `<span class="pill">${escapeHtml(item.year)}</span>` : ""}
            ${item.media_type ? `<span class="pill">${escapeHtml(item.media_type === "tv" ? "TV" : "Movie")}</span>` : ""}
            ${item.rating ? `<span class="pill">★ ${escapeHtml(Number(item.rating || 0).toFixed(1))}</span>` : ""}
          </div>
        </div>
        ${currentMediaView() === "discover" && highlightItems.length ? `
          <div class="library-highlight-controls">
            <button class="library-highlight-arrow" type="button" id="hero-prev-btn" aria-label="Previous">‹</button>
            <div class="library-highlight-dots">
              ${highlightItems.map((_, index) => `<span class="library-highlight-dot ${index === (window.__heroCarouselIndex || 0) ? "is-active" : ""}"></span>`).join("")}
            </div>
            <button class="library-highlight-arrow" type="button" id="hero-next-btn" aria-label="Next">›</button>
          </div>
        ` : ""}
      `;
      const stage = document.getElementById("library-highlight-stage");
      if (stage) {
        stage.classList.remove("is-animating");
        void stage.offsetWidth;
        stage.classList.add("is-animating");
        stage.addEventListener("click", () => openMediaModal(item));
      }
      document.getElementById("hero-prev-btn")?.addEventListener("click", () => setHeroCarouselIndex((window.__heroCarouselIndex || 0) - 1));
      document.getElementById("hero-next-btn")?.addEventListener("click", () => setHeroCarouselIndex((window.__heroCarouselIndex || 0) + 1));
    }

    function downloadIconSvg() {
      return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v11"/><path d="M8 11l4 4 4-4"/><path d="M5 19h14"/></svg>';
    }

    function spinnerIconSvg() {
      return '<svg class="spinner-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.2-8.6"/></svg>';
    }

    function checkIconSvg() {
      return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';
    }

    function jellyfinIconSvg() {
      const suffix = Math.random().toString(36).slice(2);
      const gradientA = `jellyfin-a-${suffix}`;
      const gradientB = `jellyfin-b-${suffix}`;
      return `<svg class="jellyfin-dashboard-icon" xmlns="http://www.w3.org/2000/svg" xml:space="preserve" viewBox="0 0 512 512" aria-hidden="true"><linearGradient id="${gradientA}" x1="97.508" x2="522.069" y1="308.135" y2="63.019" gradientTransform="matrix(1 0 0 -1 0 514)" gradientUnits="userSpaceOnUse"><stop offset="0" style="stop-color:#aa5cc3"/><stop offset="1" style="stop-color:#00a4dc"/></linearGradient><path d="M256 196.2c-22.4 0-94.8 131.3-83.8 153.4s156.8 21.9 167.7 0-61.3-153.4-83.9-153.4" style="fill:url(#${gradientA})"/><linearGradient id="${gradientB}" x1="94.193" x2="518.754" y1="302.394" y2="57.278" gradientTransform="matrix(1 0 0 -1 0 514)" gradientUnits="userSpaceOnUse"><stop offset="0" style="stop-color:#aa5cc3"/><stop offset="1" style="stop-color:#00a4dc"/></linearGradient><path d="M256 0C188.3 0-29.8 395.4 3.4 462.2s472.3 66 505.2 0S323.8 0 256 0m165.6 404.3c-21.6 43.2-309.3 43.8-331.1 0S211.7 101.4 256 101.4 443.2 361 421.6 404.3" style="fill:url(#${gradientB})"/></svg>`;
    }

    function mediaCard(item) {
      const typeLabel = item.media_type === "tv" ? "TV Show" : "Movie";
      const modalPayload = encodeURIComponent(JSON.stringify(item));
      return `
        <article class="media-card" data-media-modal="${escapeHtml(modalPayload)}">
          <div class="media-card-poster">
            ${item.poster_url ? `<img src="${escapeHtml(item.poster_url)}" alt="${escapeHtml(item.title || "Poster")}">` : ""}
            <span class="media-card-badge">${escapeHtml(typeLabel)}</span>
          </div>
          <div class="media-card-body">
            <h4 class="media-card-title">${escapeHtml(item.title || "Untitled")}</h4>
            <div class="media-card-meta">
              ${item.year ? `<span>${escapeHtml(item.year)}</span>` : ""}
              ${item.rating ? `<span>★ ${escapeHtml(Number(item.rating || 0).toFixed(1))}</span>` : ""}
            </div>
          </div>
        </article>
      `;
    }

    function renderMediaModalActionButton(payload, owned, jellyfinUrl) {
      const task = activeMediaTaskForPayload(payload);
      if (task) {
        return `<a class="media-modal-action-icon is-task-status is-busy" href="${escapeHtml(downloadsTaskUrl(task.id))}" title="Open download">${spinnerIconSvg()}<span>${escapeHtml(taskStatusLabel(task))}</span></a>`;
      }
      if (activeAutoDownloadRequestForPayload(payload)) {
        return `<button class="media-modal-action-icon is-busy" type="button" title="Auto download running" aria-label="Auto download running" disabled>${spinnerIconSvg()}</button>`;
      }
      if (owned && jellyfinUrl) {
        return `<a class="media-modal-action-icon is-owned" href="${escapeHtml(jellyfinUrl)}" target="_blank" rel="noreferrer" title="Open in Jellyfin">${jellyfinIconSvg()}</a>`;
      }
      return `<button class="media-modal-action-icon" type="button" data-media-autofind="${escapeHtml(encodeURIComponent(JSON.stringify(payload)))}" title="Download">${downloadIconSvg()}</button>`;
    }

    function episodeStatusKey(seasonNumber, episodeNumber) {
      return `episode:${seasonNumber}:${episodeNumber}`;
    }

    function episodeIsDownloaded(season, episode, localStatuses = {}, fallbackSeasonNumber = 1) {
      const seasonNumber = episode.season_number || season.season_number || fallbackSeasonNumber;
      const episodeNumber = episode.episode_number || 1;
      return !!localStatuses[episodeStatusKey(seasonNumber, episodeNumber)]?.exists;
    }

    function downloadedEpisodeCount(season, localStatuses = {}, fallbackSeasonNumber = 1) {
      return (season.episodes || []).filter((episode) => episodeIsDownloaded(season, episode, localStatuses, fallbackSeasonNumber)).length;
    }

    function seasonIsFullyDownloaded(season, localStatuses = {}, fallbackSeasonNumber = 1) {
      const episodes = season.episodes || [];
      return episodes.length > 0 && episodes.every((episode) => episodeIsDownloaded(season, episode, localStatuses, fallbackSeasonNumber));
    }

    function showIsFullyDownloaded(details, localStatuses = {}) {
      const seasonsWithEpisodes = (details?.seasons || []).filter((season) => (season.episodes || []).length > 0);
      return seasonsWithEpisodes.length > 0 && seasonsWithEpisodes.every((season, index) => seasonIsFullyDownloaded(season, localStatuses, season.season_number || index + 1));
    }

    function renderTvDetailActions(item, details, owned, localStatuses = {}) {
      const seasons = details?.seasons || [];
      if (!seasons.length) {
        return '<div class="media-detail-empty">No season data available yet.</div>';
      }
      return `
        <div class="media-detail-list">
          ${seasons.map((season, index) => `
            <details class="media-detail-season" data-season-key="${escapeHtml(String(season.season_number || index + 1))}">
              <summary class="media-detail-row">
                <div class="media-detail-row-main">
                  <svg class="media-detail-arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
                  <div class="media-detail-title">${escapeHtml(`S${String(season.season_number || index + 1).padStart(2, "0")}`)}</div>
                </div>
                <div class="media-detail-row-actions">
                  <span class="media-detail-count">${escapeHtml(`${downloadedEpisodeCount(season, localStatuses, season.season_number || index + 1)}/${season.episode_count || (season.episodes || []).length || 0}`)}</span>
                  ${(() => {
                    const seasonNumber = season.season_number || index + 1;
                    const key = `season:${seasonNumber}`;
                    const localOwned = seasonIsFullyDownloaded(season, localStatuses, seasonNumber);
                    const url = season.jellyfin_url || localStatuses[key]?.jellyfin_url || owned?.jellyfin_url || details?.jellyfin_url || jellyfinSearchUrl(item.title || season.search_hint || "");
                    const episodePayloads = (season.episodes || []).map((episode) => {
                      const episodeSeasonNumber = episode.season_number || seasonNumber;
                      const episodeNumber = episode.episode_number || 1;
                      const episodeKey = episodeStatusKey(episodeSeasonNumber, episodeNumber);
                      if (localStatuses[episodeKey]?.exists) return null;
                      return {
                        title: item.title || episode.search_hint || "",
                        year: item.year || "",
                        media_type: "tv",
                        season: episodeSeasonNumber,
                        episode: episodeNumber,
                        poster_url: item.poster_url || "",
                        backdrop_url: item.backdrop_url || "",
                      };
                    }).filter(Boolean);
                    return renderMediaModalActionButton(
                      {
                        title: item.title || season.search_hint || "",
                        year: item.year || "",
                        media_type: "tv",
                        season: seasonNumber,
                        episode: 1,
                        poster_url: item.poster_url || "",
                        backdrop_url: item.backdrop_url || "",
                        batch_items: episodePayloads,
                      },
                      localOwned,
                      url
                    );
                  })()}
                </div>
              </summary>
              <div class="media-detail-episodes">
                ${(season.episodes || []).map((episode) => `
                  <div class="media-detail-episode-row">
                    <div class="media-detail-episode-title">${escapeHtml(`E${String(episode.episode_number || 0).padStart(2, "0")} • ${episode.title || "Episode"}`)}</div>
                    <div class="media-detail-row-actions">
                      <span class="media-detail-date">${escapeHtml(episode.air_date || "")}</span>
                      ${(() => {
                    const seasonNumber = episode.season_number || season.season_number || index + 1;
                    const episodeNumber = episode.episode_number || 1;
                        const key = episodeStatusKey(seasonNumber, episodeNumber);
                        const localOwned = !!localStatuses[key]?.exists;
                        const url = episode.jellyfin_url || localStatuses[key]?.jellyfin_url || owned?.jellyfin_url || details?.jellyfin_url || jellyfinSearchUrl(item.title || episode.search_hint || "");
                        return renderMediaModalActionButton(
                          {
                            title: item.title || episode.search_hint || "",
                            year: item.year || "",
                            media_type: "tv",
                            season: seasonNumber,
                            episode: episodeNumber,
                            poster_url: item.poster_url || "",
                            backdrop_url: item.backdrop_url || "",
                          },
                          localOwned,
                          url
                        );
                      })()}
                    </div>
                  </div>
                `).join("")}
              </div>
            </details>
          `).join("")}
        </div>
      `;
    }

    function captureOpenSeasonKeys(scope) {
      return new Set(Array.from(scope?.querySelectorAll(".media-detail-season[open][data-season-key]") || []).map((season) => season.dataset.seasonKey));
    }

    function restoreOpenSeasonKeys(scope, openKeys) {
      if (!openKeys?.size) return;
      scope?.querySelectorAll(".media-detail-season[data-season-key]").forEach((season) => {
        if (openKeys.has(season.dataset.seasonKey)) {
          season.open = true;
        }
      });
    }

    function bindSeasonToggleArrows(scope) {
      scope.querySelectorAll(".media-detail-season").forEach((season) => {
        const arrow = season.querySelector(".media-detail-arrow");
        if (!arrow) return;
        const sync = () => {
          arrow.innerHTML = season.hasAttribute("open")
            ? '<path d="M6 9l6 6 6-6"/>'
            : '<path d="M9 6l6 6-6 6"/>';
        };
        sync();
        season.addEventListener("toggle", sync);
      });
    }

    function renderOpenMediaModalActions() {
      const meta = document.getElementById("media-modal-meta");
      const actions = document.getElementById("media-modal-actions");
      const item = window.__openMediaItem;
      if (!meta || !actions || !item) return;
      if (!window.__openMediaDetails || !window.__openMediaLocalStatuses) return;
      const owned = null;
      const details = window.__openMediaDetails;
      const localStatuses = window.__openMediaLocalStatuses;
      const isTv = (item.media_type || "movie") === "tv";
      const primaryLocalOwned = isTv ? showIsFullyDownloaded(details, localStatuses) : !!localStatuses.primary?.exists;
      const primaryAction = renderMediaModalActionButton(
        {
          title: item.title || "",
          year: item.year || "",
          media_type: item.media_type || "movie",
          poster_url: item.poster_url || "",
          backdrop_url: item.backdrop_url || "",
        },
        owned || primaryLocalOwned,
        owned?.jellyfin_url || details?.jellyfin_url || localStatuses.primary?.jellyfin_url || jellyfinSearchUrl(item.title || "")
      );
      const metaHtml = `
        <div class="media-modal-meta">
          <div class="library-highlight-meta">
            ${item.media_type ? `<span class="pill">${escapeHtml(item.media_type === "tv" ? "TV Show" : "Movie")}</span>` : ""}
            ${item.year ? `<span class="pill">${escapeHtml(item.year)}</span>` : ""}
            ${item.rating ? `<span class="pill">★ ${escapeHtml(Number(item.rating || 0).toFixed(1))}</span>` : ""}
          </div>
          <div class="media-modal-actions">${primaryAction}</div>
        </div>
      `;
      if (meta.innerHTML !== metaHtml) {
        meta.innerHTML = metaHtml;
        bindMediaActions(meta);
      }
      if (isTv) {
        const openKeys = captureOpenSeasonKeys(actions);
        const actionsHtml = renderTvDetailActions(item, details, owned, localStatuses);
        if (actions.innerHTML !== actionsHtml) {
          actions.innerHTML = actionsHtml;
          restoreOpenSeasonKeys(actions, openKeys);
          bindSeasonToggleArrows(actions);
          bindMediaActions(actions);
        }
      } else {
        if (actions.innerHTML) {
          actions.innerHTML = "";
        }
      }
    }

    async function openMediaModal(item) {
      const modal = document.getElementById("media-modal");
      const hero = document.getElementById("media-modal-hero");
      const poster = document.getElementById("media-modal-poster");
      const meta = document.getElementById("media-modal-meta");
      const title = document.getElementById("media-modal-title");
      const overview = document.getElementById("media-modal-overview");
      const actions = document.getElementById("media-modal-actions");
      if (!modal || !hero || !poster || !meta || !title || !overview || !actions) return;
      const backdrop = item.backdrop_url ? `linear-gradient(180deg, rgba(7,16,26,0.08), rgba(7,16,26,0.94)), url("${item.backdrop_url}")` : "";
      hero.style.backgroundImage = backdrop || "";
      poster.innerHTML = item.poster_url ? `<img src="${escapeHtml(item.poster_url)}" alt="${escapeHtml(item.title || "Poster")}">` : "";
      meta.innerHTML = `
        <div class="library-highlight-meta">
          ${item.media_type ? `<span class="pill">${escapeHtml(item.media_type === "tv" ? "TV Show" : "Movie")}</span>` : ""}
          ${item.year ? `<span class="pill">${escapeHtml(item.year)}</span>` : ""}
          ${item.rating ? `<span class="pill">★ ${escapeHtml(Number(item.rating || 0).toFixed(1))}</span>` : ""}
        </div>
      `;
      title.textContent = item.title || "Untitled";
      overview.textContent = (item.overview || "").trim() || "No overview available.";
      actions.innerHTML = '<div class="media-detail-empty">Loading details…</div>';
      modal.classList.add("is-open");
      modal.setAttribute("aria-hidden", "false");
      document.body.style.overflow = "hidden";
      window.__openMediaItem = item;
      window.__openMediaDetails = null;
      window.__openMediaLocalStatuses = null;
      let details = { seasons: [] };
      try {
        const detailsResponse = await fetch(`/api/media/details?provider=${encodeURIComponent(item.provider || "tmdb")}&provider_id=${encodeURIComponent(item.provider_id || "")}&media_type=${encodeURIComponent(item.media_type || "movie")}`);
        details = detailsResponse.ok ? await detailsResponse.json() : { seasons: [] };
      } catch (_error) {
        details = { seasons: [] };
      }
      const statusRequests = [
        {
          key: "primary",
          payload: {
            title: item.title || "",
            year: item.year || "",
            media_type: item.media_type || "movie"
          }
        }
      ];
      if ((item.media_type || "movie") === "tv") {
        (details.seasons || []).forEach((season, index) => {
          const seasonNumber = season.season_number || index + 1;
          statusRequests.push({
            key: `season:${seasonNumber}`,
            payload: {
              title: item.title || season.search_hint || "",
              year: item.year || "",
              media_type: "tv",
              season: seasonNumber
            }
          });
          (season.episodes || []).forEach((episode) => {
            const episodeNumber = episode.episode_number || 1;
            const episodeSeasonNumber = episode.season_number || seasonNumber;
            statusRequests.push({
              key: `episode:${episodeSeasonNumber}:${episodeNumber}`,
              payload: {
                title: item.title || episode.search_hint || "",
                year: item.year || "",
                media_type: "tv",
                season: episodeSeasonNumber,
                episode: episodeNumber
              }
            });
          });
        });
      }
      const localStatuses = await fetchLocalMediaStatuses(statusRequests);
      if (window.__openMediaItem?.id !== item.id) {
        return;
      }
      window.__openMediaDetails = details;
      window.__openMediaLocalStatuses = localStatuses;
      renderOpenMediaModalActions();
    }

    function closeMediaModal() {
      const modal = document.getElementById("media-modal");
      if (!modal) return;
      modal.classList.remove("is-open");
      modal.setAttribute("aria-hidden", "true");
      document.body.style.overflow = "";
      window.__openMediaItem = null;
      window.__openMediaDetails = null;
      window.__openMediaLocalStatuses = null;
    }

    function readSessionJson(key, fallback) {
      try {
        const raw = sessionStorage.getItem(key);
        return raw ? JSON.parse(raw) : fallback;
      } catch (_error) {
        return fallback;
      }
    }

    function writeSessionJson(key, value) {
      try {
        sessionStorage.setItem(key, JSON.stringify(value));
      } catch (_error) {
        // Session persistence is a convenience; the UI still works without it.
      }
    }

    function restoreAutoDownloadRequests() {
      if (!Array.isArray(window.__autoDownloadRequests)) {
        window.__autoDownloadRequests = readSessionJson(AUTO_DOWNLOAD_REQUESTS_KEY, []);
      }
      return window.__autoDownloadRequests;
    }

    function persistAutoDownloadRequests() {
      writeSessionJson(AUTO_DOWNLOAD_REQUESTS_KEY, window.__autoDownloadRequests || []);
    }

    function failInterruptedAutoDownloadRequests() {
      const requests = restoreAutoDownloadRequests();
      let changed = false;
      requests.forEach((request) => {
        const status = String(request?.status || "").toLowerCase();
        if (!request || !["starting", "searching", "queued", "running"].includes(status)) {
          return;
        }
        request.status = "failed";
        request.finished_logged = true;
        request.logs = Array.isArray(request.logs) ? request.logs : [];
        request.logs.push(`${new Date().toLocaleTimeString()} Auto-download request failed because the app restarted before a stream was queued.`);
        changed = true;
      });
      if (changed) {
        persistAutoDownloadRequests();
      }
    }

    function handleSystemStartupChange(summary) {
      const startupId = String(summary?.startup_id || "");
      if (!startupId) return;
      if (!window.__systemStartupId) {
        window.__systemStartupId = startupId;
        return;
      }
      if (window.__systemStartupId === startupId) {
        return;
      }
      window.__systemStartupId = startupId;
      failInterruptedAutoDownloadRequests();
      renderAutoDownloadRequests();
      renderOpenMediaModalActions();
    }

    function deleteAutoDownloadRequest(requestId) {
      const id = String(requestId || "");
      if (!id) return;
      window.__autoDownloadRequests = restoreAutoDownloadRequests().filter((request) => request?.id !== id);
      persistAutoDownloadRequests();
      renderAutoDownloadRequests();
      renderOpenMediaModalActions();
    }

    function activeAutoDownloadToasts() {
      const now = Date.now();
      const active = readSessionJson(AUTO_DOWNLOAD_TOASTS_KEY, []).filter((toast) => Number(toast.expires_at || 0) > now);
      writeSessionJson(AUTO_DOWNLOAD_TOASTS_KEY, active);
      return active;
    }

    function upsertAutoDownloadToast(toast) {
      const active = activeAutoDownloadToasts().filter((item) => item.id !== toast.id);
      active.push(toast);
      writeSessionJson(AUTO_DOWNLOAD_TOASTS_KEY, active);
    }

    function removeAutoDownloadToast(id) {
      writeSessionJson(AUTO_DOWNLOAD_TOASTS_KEY, activeAutoDownloadToasts().filter((toast) => toast.id !== id));
    }

    function showAutoDownloadToast(title, copy, options = {}) {
      const stack = document.getElementById("auto-download-toasts");
      if (!stack) return null;
      const id = options.id || `toast-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const createdAt = Number(options.created_at || Date.now());
      const expiresAt = Number(options.expires_at || createdAt + AUTO_DOWNLOAD_TOAST_DURATION);
      const href = String(options.href || "");
      if (options.persist !== false) {
        upsertAutoDownloadToast({ id, title, copy, href, created_at: createdAt, expires_at: expiresAt });
      }
      const elapsed = Math.max(0, Date.now() - createdAt);
      const toast = document.createElement("div");
      toast.className = `auto-download-toast${href ? " has-link" : ""}`;
      toast.innerHTML = `
        <button class="auto-download-close" type="button" aria-label="Dismiss">×</button>
        <div class="auto-download-title">${escapeHtml(title || "Auto download")}</div>
        <div class="auto-download-copy">${escapeHtml(copy || "Started in the background.")}</div>
        <div class="auto-download-progress" style="animation-duration: ${AUTO_DOWNLOAD_TOAST_DURATION}ms; animation-delay: -${Math.min(elapsed, AUTO_DOWNLOAD_TOAST_DURATION)}ms;"></div>
      `;
      stack.appendChild(toast);
      let timer = null;
      const remove = () => {
        if (timer) clearTimeout(timer);
        removeAutoDownloadToast(id);
        toast.remove();
      };
      toast.querySelector(".auto-download-close")?.addEventListener("click", remove);
      if (href) {
        toast.addEventListener("click", (event) => {
          if (event.target.closest(".auto-download-close")) return;
          window.location.href = href;
        });
      }
      timer = setTimeout(remove, Math.max(0, expiresAt - Date.now()));
      return {
        dismiss: remove
      };
    }

    function restoreActiveAutoDownloadToasts() {
      activeAutoDownloadToasts().forEach((toast) => {
        showAutoDownloadToast(toast.title, toast.copy, { ...toast, persist: false });
      });
    }

    function taskMatchesAutoFind(task, payload) {
      const expectedTitle = cleanMediaTitle(payload?.title || "").toLowerCase();
      const taskTitle = cleanMediaTitle(task?.title || task?.series_name || "").toLowerCase();
      if (expectedTitle && taskTitle && !taskTitle.includes(expectedTitle) && !expectedTitle.includes(taskTitle)) {
        return false;
      }
      if (payload?.season && String(task?.season || task?.metadata?.season || "") !== String(payload.season)) {
        return false;
      }
      if (payload?.episode && String(task?.episode || task?.metadata?.episode || "") !== String(payload.episode)) {
        return false;
      }
      return ["queued", "running", "completed", "failed", "stopped"].includes(String(task?.status || ""));
    }

    function autoDownloadLabel(payload) {
      const parts = [payload?.title || "Auto download"];
      if (payload?.season && payload?.episode) {
        parts.push(`S${String(payload.season).padStart(2, "0")}E${String(payload.episode).padStart(2, "0")}`);
      }
      return parts.join(" ");
    }

    function autoDownloadEpisodeCode(payload) {
      if (!payload?.season || !payload?.episode) return "";
      return `S${String(payload.season).padStart(2, "0")}E${String(payload.episode).padStart(2, "0")}`;
    }

    function sleep(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function autoFindBatchDelayMs() {
      const site = String(browserSiteSelect?.value || "yflix").toLowerCase();
      return site === "yflix" ? 25000 : 5000;
    }

    function addAutoDownloadRequest(payload) {
      restoreAutoDownloadRequests();
      const createdAt = new Date();
      const request = {
        id: `auto-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        title: autoDownloadLabel(payload),
        status: "starting",
        created_at: createdAt.toISOString(),
        logs: [`${createdAt.toLocaleTimeString()} Created auto-download request for ${autoDownloadLabel(payload)}.`],
        payload,
      };
      window.__autoDownloadRequests.unshift(request);
      persistAutoDownloadRequests();
      renderAutoDownloadRequests();
      renderOpenMediaModalActions();
      return request;
    }

    function appendAutoDownloadLog(request, message, status) {
      if (!request) return;
      if (status) request.status = status;
      request.logs.push(`${new Date().toLocaleTimeString()} ${message}`);
      persistAutoDownloadRequests();
      renderAutoDownloadRequests();
      renderOpenMediaModalActions();
    }

    function captureAutoRequestUiState(scope) {
      const state = {};
      scope?.querySelectorAll(".auto-request-item[data-request-id]").forEach((row) => {
        const requestId = row.dataset.requestId;
        if (!requestId) return;
        state[requestId] = {
          open: !!row.open,
          logScrollTop: row.querySelector(".auto-request-log")?.scrollTop || 0
        };
      });
      return state;
    }

    function restoreAutoRequestUiState(scope, state) {
      scope?.querySelectorAll(".auto-request-item[data-request-id]").forEach((row) => {
        const saved = state[row.dataset.requestId];
        if (!saved) return;
        row.open = !!saved.open;
        const log = row.querySelector(".auto-request-log");
        if (log) log.scrollTop = saved.logScrollTop || 0;
      });
    }

    function autoRequestStatusClass(status) {
      if (status === "completed" || status === "queued") return "completed";
      if (status === "failed" || status === "stopped") return "failed";
      return "queued";
    }

    function reconcileAutoDownloadRequests() {
      const requests = restoreAutoDownloadRequests();
      if (!requests.length || !window.__taskState) return;
      let changed = false;
      requests.forEach((request) => {
        if (!request || ["completed", "failed", "stopped"].includes(request.status)) return;
        const match = allTasks().find((task) => taskMatchesAutoFind(task, request.payload || {}));
        if (!match) return;
        const status = String(match.status || "queued");
        const alreadyLinked = request.task_id === match.id;
        request.task_id = match.id;
        if (status === "failed" || status === "stopped") {
          request.status = status;
          if (!request.finished_logged) {
            request.logs.push(`${new Date().toLocaleTimeString()} Download ${status}: ${match.error || match.title || match.id}.`);
            request.finished_logged = true;
          }
        } else {
          request.status = "completed";
          if (!alreadyLinked || !request.queued_logged) {
            request.logs.push(`${new Date().toLocaleTimeString()} Real download triggered as ${match.title || match.id} (${status}).`);
            request.logs.push(`${new Date().toLocaleTimeString()} Auto-download request complete; follow progress on the Downloads page.`);
            request.queued_logged = true;
            request.completed_logged = true;
          }
        }
        changed = true;
      });
      if (changed) {
        persistAutoDownloadRequests();
        renderAutoDownloadRequests();
      }
    }

    function formatAutoDownloadTimestamp(value) {
      const date = value ? new Date(value) : null;
      if (!date || Number.isNaN(date.getTime())) {
        return "";
      }
      return date.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
      });
    }

    function renderAutoDownloadRequests() {
      const list = document.getElementById("auto-download-request-list");
      if (!list) return;
      const uiState = captureAutoRequestUiState(list);
      const requests = restoreAutoDownloadRequests();
      if (!requests.length) {
        list.innerHTML = '<div class="empty">No auto-download requests are running.</div>';
        return;
      }
      list.innerHTML = requests.map((request) => `
        <details class="auto-request-item" data-request-id="${escapeHtml(request.id || "")}">
          <summary class="auto-request-top">
            <time class="auto-request-time">${escapeHtml(formatAutoDownloadTimestamp(request.created_at))}</time>
            <div class="auto-request-title">${escapeHtml(request.title)}</div>
            <span class="status-pill ${escapeHtml(autoRequestStatusClass(request.status))}">${escapeHtml(request.status)}</span>
            <button class="auto-request-delete" type="button" data-auto-request-delete="${escapeHtml(request.id || "")}" aria-label="Delete auto-download request" title="Delete">×</button>
          </summary>
          <pre class="auto-request-log">${escapeHtml((request.logs || []).join("\\n"))}</pre>
        </details>
      `).join("");
      restoreAutoRequestUiState(list, uiState);
      list.querySelectorAll("[data-auto-request-delete]").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          deleteAutoDownloadRequest(button.dataset.autoRequestDelete);
        });
      });
    }

    function waitForAutoDownloadQueued(payload, existingIds, request) {
      const startedAt = Date.now();
      let attempts = 0;
      appendAutoDownloadLog(request, "Polling Downloads for a newly queued task.", "searching");
      return new Promise((resolve) => {
        const timer = setInterval(async () => {
          attempts += 1;
          try {
            const response = await fetch("/api/tasks");
            if (response.ok) {
              window.__taskState = await response.json();
              notifyTaskStatusChanges();
              renderTasks();
              const match = allTasks().find((task) => !existingIds.has(task.id) && taskMatchesAutoFind(task, payload));
              if (match) {
                clearInterval(timer);
                request.task_id = match.id;
                request.queued_logged = true;
                request.completed_logged = true;
                appendAutoDownloadLog(request, `Real download triggered as ${match.title || match.id} (${match.status || "queued"}).`, "completed");
                appendAutoDownloadLog(request, "Auto-download request complete; follow progress on the Downloads page.", "completed");
                resolve(match);
                return;
              }
              if (attempts === 1 || attempts % 5 === 0) {
                appendAutoDownloadLog(request, `Still waiting for a matching queued task (${attempts} checks).`, "searching");
              }
            }
          } catch (error) {
            if (attempts === 1 || attempts % 5 === 0) {
              appendAutoDownloadLog(request, `Task poll failed; retrying (${String(error).slice(0, 120)}).`, "searching");
            }
          }
          if (Date.now() - startedAt > 45000) {
            clearInterval(timer);
            appendAutoDownloadLog(request, "Timed out waiting for a queued task. Continuing with the next request if this is a batch.", "failed");
            resolve(null);
          }
        }, 1400);
      });
    }

    async function startAutoFindPayload(payload, existingIds, context = {}) {
      const request = addAutoDownloadRequest(payload);
      const code = autoDownloadEpisodeCode(payload);
      const prefix = context.total ? `Batch ${context.index}/${context.total}${code ? ` ${code}` : ""}: ` : "";
      appendAutoDownloadLog(request, `${prefix}Preparing remote browser request.`, "starting");
      appendAutoDownloadLog(request, `Title: ${payload.title || "Untitled"}${payload.year ? ` (${payload.year})` : ""}.`, "starting");
      if (code) {
        appendAutoDownloadLog(request, `Episode target: ${code}.`, "starting");
      }
      appendAutoDownloadLog(request, `Site: ${browserSiteSelect?.value || "yflix"}.`, "starting");
      const response = await fetch("/api/media/auto-find", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, site: browserSiteSelect?.value || "yflix" })
      });
      if (!response.ok) {
        appendAutoDownloadLog(request, `Remote browser request failed with HTTP ${response.status}.`, "failed");
        throw new Error(`Auto-find failed with ${response.status}`);
      }
      const result = await response.json();
      appendAutoDownloadLog(request, `Browser command ${result.browser_command_id || ""} issued for ${result.site}.`, "searching");
      appendAutoDownloadLog(request, `Search hint: ${result.search_hint || payload.title || "title"}.`, "searching");
      if (result.target_url) {
        appendAutoDownloadLog(request, `Target URL: ${result.target_url}.`, "searching");
      }
      appendAutoDownloadLog(request, "Waiting for the stream detector to report an HLS playlist and queue a real download.", "searching");
      try {
        await navigator.clipboard.writeText(result.search_hint || payload.title || "");
        appendAutoDownloadLog(request, "Copied search hint to clipboard.", "searching");
      } catch (_error) {
        appendAutoDownloadLog(request, "Clipboard copy skipped or unavailable.", "searching");
      }
      const queuedTask = await waitForAutoDownloadQueued(payload, existingIds, request);
      if (queuedTask?.id) {
        existingIds.add(queuedTask.id);
      }
      return queuedTask;
    }

    function bindMediaActions(scope) {
      scope.querySelectorAll("[data-media-modal]").forEach((node) => {
        node.addEventListener("click", (event) => {
          event.stopPropagation();
          const encoded = node.dataset.mediaModal;
          if (!encoded) return;
          openMediaModal(JSON.parse(decodeURIComponent(encoded)));
        });
      });
      scope.querySelectorAll("[data-media-autofind]").forEach((button) => {
        button.addEventListener("click", async () => {
          const encoded = button.dataset.mediaAutofind;
          if (!encoded) return;
          const payload = JSON.parse(decodeURIComponent(encoded));
          const payloads = autoFindItemsForPayload(payload);
          const existingIds = new Set(allTasks().map((task) => task.id));
          const toastTitle = payloads.length > 1 ? `${payload.title || "Season"} downloads` : payload.title || "Auto download";
          const toastCopy = payloads.length > 1
            ? `${payloads.length} episode downloads queued with pacing to avoid site rate limits.`
            : "Auto download started in the background.";
          showAutoDownloadToast(toastTitle, toastCopy);
          button.disabled = true;
          try {
            const results = [];
            const batchDelay = payloads.length > 1 ? autoFindBatchDelayMs() : 0;
            for (const [index, item] of payloads.entries()) {
              try {
                const queuedTask = await startAutoFindPayload(item, existingIds, {
                  index: index + 1,
                  total: payloads.length,
                });
                renderOpenMediaModalActions();
                results.push({ status: "fulfilled", value: queuedTask });
              } catch (error) {
                console.error(error);
                showAutoDownloadToast(
                  "Episode auto download failed",
                  `${autoDownloadEpisodeCode(item) || item.title || "Episode"} failed; continuing batch.`
                );
                results.push({ status: "fulfilled", value: null });
              }
              if (batchDelay && index < payloads.length - 1) {
                showAutoDownloadToast(
                  `${payload.title || "Season"} batch paused`,
                  `Waiting ${Math.round(batchDelay / 1000)} seconds before the next episode to avoid site rate limits.`
                );
                await sleep(batchDelay);
              }
            }
            const triggeredCount = results.filter((result) => result.status === "fulfilled" && result.value).length;
            if (payloads.length > 1) {
              showAutoDownloadToast(
                `${payload.title || "Season"} batch finished`,
                `${triggeredCount}/${payloads.length} episode requests triggered real downloads.`
              );
            }
          } finally {
            button.disabled = false;
          }
        });
      });
    }

    function renderMediaSections() {
      const container = document.getElementById("media-sections");
      if (!container) return;
      if (currentMediaView() === "download") {
        container.innerHTML = "";
        renderMediaHighlight();
        return;
      }
      const isSearchView = currentMediaView() === "search";
      const state = isSearchView ? window.__searchState : window.__discoverState;
      if (isSearchView && !state) {
        window.__searchState = { configured: true, query: "", sections: [], source: "tmdb" };
        renderMediaSections();
        return;
      }
      if (!state?.configured && !(state?.sections || []).length) {
        container.innerHTML = '<section class="library-section"><div class="media-card-empty">TMDb is not configured or not reachable through Mullvad yet.</div></section>';
        renderMediaHighlight();
        return;
      }
      const sections = state.sections || [];
      if (isSearchView) {
        const query = (state?.query || "").trim();
        const mergedItems = sections.flatMap((section) => section.items || []);
        container.innerHTML = `
          <section class="library-section">
            <header class="panel-header premium-header">
              <div>
                <h3 class="section-title">${query ? `Results for "${escapeHtml(query)}"` : "Search"}</h3>
              </div>
            </header>
            <div class="media-grid">
              ${query ? (mergedItems.length ? mergedItems.map(mediaCard).join("") : '<div class="media-card-empty">No matching items found.</div>') : '<div class="media-card-empty">Search for a movie or TV show.</div>'}
            </div>
          </section>
        `;
        bindMediaActions(container);
        renderMediaHighlight();
        setMediaArtworkMap();
        renderTasks();
        return;
      }
      container.innerHTML = sections.map((section) => `
        <section class="library-section">
          <header class="panel-header premium-header">
            <div>
              <h3 class="section-title">${escapeHtml(section.title || "Section")}</h3>
            </div>
          </header>
          <div class="media-rail">
            ${(section.items || []).length ? (section.items || []).map(mediaCard).join("") : '<div class="media-card-empty">No items in this section.</div>'}
          </div>
        </section>
      `).join("");
      bindMediaActions(container);
      renderMediaHighlight();
      setMediaArtworkMap();
      renderTasks();
    }

    async function refreshMediaData(force = false) {
      if (activePageName() !== "library" && !force) {
        return;
      }
      if (currentMediaView() === "search") {
        const query = document.getElementById("media-search-input")?.value?.trim() || "";
        if (!query) {
          window.__searchState = { configured: true, query: "", sections: [], source: "tmdb" };
          renderMediaSections();
          return;
        }
        const searchResponse = await fetch(`/api/media/discover?query=${encodeURIComponent(query)}`);
        window.__searchState = searchResponse.ok ? await searchResponse.json() : { configured: false, query, sections: [] };
      } else {
        const discoverResponse = await fetch("/api/media/discover");
        window.__discoverState = discoverResponse.ok ? await discoverResponse.json() : { configured: false, query: "", sections: [] };
      }
      renderMediaSections();
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

    function applyMullvadUiState(connected) {
      const outboundIds = [
        "media-search-submit",
        "youtube-lookup-btn",
        "youtube-refresh-btn",
        "youtube-subscribe-btn",
        "youtube-queue-btn",
        "music-refresh-queue"
      ];
      outboundIds.forEach((id) => {
        const node = document.getElementById(id);
        if (node) {
          node.disabled = !connected;
          node.title = connected ? "" : "Connect Mullvad before using internet features";
        }
      });
      const siteSelect = document.getElementById("browser-site-select");
      if (siteSelect) {
        siteSelect.disabled = !connected;
        siteSelect.title = connected ? "" : "Connect Mullvad before browsing";
      }
      document.querySelectorAll(".media-card-btn[data-media-autofind]").forEach((button) => {
        button.disabled = !connected;
        button.title = connected ? "" : "Connect Mullvad before using auto-find";
      });
      renderBrowserAvailability(connected);
    }

    function renderBrowserAvailability(connected) {
      const blocked = document.getElementById("browser-blocked");
      const blockedCopy = document.getElementById("browser-blocked-copy");
      if (!browserFrame || !blocked) {
        return;
      }
      if (!connected) {
        browserFrame.classList.add("is-hidden");
        if (browserFrame.src !== "about:blank") {
          browserFrame.src = "about:blank";
        }
        blocked.classList.add("is-visible");
        if (blockedCopy) {
          blockedCopy.textContent = "Connect Mullvad to start the remote browser and reach internet destinations.";
        }
        window.__browserWasBlocked = true;
        return;
      }

      blocked.classList.remove("is-visible");
      browserFrame.classList.remove("is-hidden");
      const desiredSrc = browserFrame.dataset.browserSrc || "__BROWSER_URL__";
      if (window.__browserWasBlocked || browserFrame.src === "about:blank") {
        browserFrame.src = desiredSrc;
      } else {
        try {
          const current = new URL(browserFrame.src);
          if (!current.hostname) {
            browserFrame.src = desiredSrc;
          }
        } catch (_error) {
          browserFrame.src = desiredSrc;
        }
      }
      window.__browserWasBlocked = false;
    }

    function renderSystemSummary() {
      const summary = window.__systemSummary;
      if (!summary) {
        return;
      }
      const free = formatBytes(summary.disk?.free);
      const total = formatBytes(summary.disk?.total);
      const storageLine = `${free} free`;
      const storageDetail = `${free} free of ${total}`;
      const usedBytes = Number(summary.disk?.used || 0);
      const totalBytes = Number(summary.disk?.total || 0);
      const usedPercent = totalBytes > 0 ? Math.max(0, Math.min(100, (usedBytes / totalBytes) * 100)) : 0;
      const connected = !!summary.mullvad?.connected;
      const mullvadBanner = document.getElementById("mullvad-banner");
      const sidebarValue = document.getElementById("sidebar-storage-value");
      const sidebarDetail = document.getElementById("sidebar-storage-detail");
      const sidebarPercent = document.getElementById("sidebar-storage-percent");
      const sidebarCapacity = document.getElementById("sidebar-storage-capacity");
      const sidebarBar = document.getElementById("sidebar-storage-bar");
      if (sidebarValue) sidebarValue.textContent = `${Math.round(usedPercent)}%`;
      if (sidebarDetail) sidebarDetail.textContent = storageDetail;
      if (sidebarPercent) sidebarPercent.textContent = `${Math.round(usedPercent)}% used`;
      if (sidebarCapacity) sidebarCapacity.textContent = `${formatBytes(usedBytes)} of ${total}`;
      if (sidebarBar) sidebarBar.style.width = `${usedPercent.toFixed(1)}%`;
      const jellyfinSummary = document.getElementById("jellyfin-settings-summary");
      const tmdbSummary = document.getElementById("tmdb-settings-summary");
      if (jellyfinSummary) {
        jellyfinSummary.textContent = summary.jellyfin?.configured
          ? `Jellyfin opens at ${summary.jellyfin.url}`
          : "Set JELLYFIN_URL to show Jellyfin handoff links for completed media.";
      }
      if (tmdbSummary) {
        tmdbSummary.textContent = summary.tmdb?.configured
          ? "TMDb discover rails are enabled."
          : "Set TMDB_API_KEY to enable discover rails and poster metadata.";
      }
      if (mullvadBanner) {
        mullvadBanner.classList.toggle("is-visible", !connected);
      }
      applyMullvadUiState(connected);
    }

    async function refreshSystemSummary() {
      const response = await fetch("/api/system/summary");
      if (!response.ok) {
        return;
      }
      window.__systemSummary = await response.json();
      handleSystemStartupChange(window.__systemSummary);
      renderSystemSummary();
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
      const mullvadConnected = !!window.__systemSummary?.mullvad?.connected;
      const streams = state.streams || [];
      document.getElementById("browser-stream-count").textContent = `${streams.length} detected`;
      const list = document.getElementById("browser-stream-list");
      if (!mullvadConnected) {
        list.innerHTML = '<div class="empty">Connect Mullvad to enable the remote browser and stream detection.</div>';
        return;
      }
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

    function guessTitleFromUrl(url) {
      try {
        const parsed = new URL(url);
        if (parsed.protocol === "magnet:") {
          return "Magnet link";
        }
        const pathname = parsed.pathname.split("/").filter(Boolean).pop() || parsed.hostname;
        return decodeURIComponent(pathname.replace(/[-_]+/g, " "));
      } catch (_error) {
        if (url.startsWith("magnet:")) {
          return "Magnet link";
        }
        return "Queued download";
      }
    }

    async function refresh() {
      if (window.__refreshInFlight) {
        return;
      }
      window.__refreshInFlight = true;
      try {
        updatePageVisibilityState();
        await refreshSystemSummary();
        const response = await fetch("/api/tasks");
        window.__taskState = await response.json();
        notifyTaskStatusChanges();
        reconcileAutoDownloadRequests();
        renderTasks();
        if ((activePageName() === "library" || activePageName() === "downloads") && !window.__mediaLoaded) {
          await refreshMediaData(true);
          window.__mediaLoaded = true;
        }
        await refreshBrowserState();
        await refreshYouTubeState();
        await refreshMusicState();
        await refreshGeneralSettings();
      } finally {
        window.__refreshInFlight = false;
      }
    }

    function handleDownloadSearchInput(value) {
      window.__downloadSearch = value || "";
      const page = document.getElementById("downloads-page-search");
      if (page && page.value !== window.__downloadSearch) page.value = window.__downloadSearch;
      renderTasks();
    }

    document.getElementById("downloads-page-search")?.addEventListener("input", (event) => {
      handleDownloadSearchInput(event.target.value || "");
    });

    document.querySelectorAll("[data-download-state-filter]").forEach((button) => {
      button.addEventListener("click", () => {
        window.__downloadStateFilter = button.dataset.downloadStateFilter || "all";
        document.querySelectorAll("[data-download-state-filter]").forEach((node) => {
          node.classList.toggle("is-active", node === button);
        });
        renderTasks();
      });
    });

    document.querySelectorAll("[data-download-type-filter]").forEach((button) => {
      button.addEventListener("click", () => {
        window.__downloadTypeFilter = button.dataset.downloadTypeFilter || "all";
        document.querySelectorAll("[data-download-type-filter]").forEach((node) => {
          node.classList.toggle("is-active", node === button);
        });
        renderTasks();
      });
    });

    document.getElementById("browser-reconnect-btn")?.addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      try {
        await refreshSystemSummary();
        if (window.__systemSummary?.mullvad?.connected) {
          renderBrowserAvailability(true);
          await refreshBrowserState();
        }
      } finally {
        event.currentTarget.disabled = false;
      }
    });

    document.querySelectorAll("[data-media-view]").forEach((button) => {
      button.addEventListener("click", async () => {
        const nextView = normalizeMediaView(button.dataset.mediaView || "discover");
        switchMediaView(nextView, { updateHistory: true, historyMode: "push" });
        if ((nextView === "discover" && !window.__discoverState) || (nextView === "search" && !window.__searchState) || !window.__mediaLoaded) {
          await refreshMediaData(true);
          window.__mediaLoaded = true;
        }
      });
    });

    document.getElementById("media-search-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      switchMediaView("search", { updateHistory: true, historyMode: "push" });
      await refreshMediaData(true);
      window.__mediaLoaded = true;
    });

    document.getElementById("media-modal-close")?.addEventListener("click", closeMediaModal);
    document.getElementById("media-modal")?.addEventListener("click", (event) => {
      if (event.target.id === "media-modal") {
        closeMediaModal();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeMediaModal();
      }
    });
    window.addEventListener("popstate", () => {
      const path = window.location.pathname;
      if (path === "/movies-tv/download") {
        switchMediaView("download", { updateHistory: false });
      } else if (path === "/movies-tv/search") {
        switchMediaView("search", { updateHistory: false });
      } else if (path === "/movies-tv/discover" || path === "/movies-tv") {
        switchMediaView("discover", { updateHistory: false });
      }
    });

    document.getElementById("mobile-nav-toggle")?.addEventListener("click", (event) => {
      const isOpen = document.body.classList.toggle("sidebar-open");
      event.currentTarget.setAttribute("aria-expanded", String(isOpen));
    });

    document.getElementById("sidebar-collapse-btn")?.addEventListener("click", () => {
      const sidebar = document.getElementById("sidebar-nav");
      if (!sidebar) return;
      const collapsed = sidebar.classList.toggle("is-collapsed");
      document.body.classList.toggle("sidebar-collapsed", collapsed);
      localStorage.setItem("isambard.sidebarCollapsed", collapsed ? "1" : "0");
      document.getElementById("sidebar-collapse-btn")?.setAttribute("aria-pressed", collapsed ? "true" : "false");
    });

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
    if (localStorage.getItem("isambard.sidebarCollapsed") !== "0") {
      document.getElementById("sidebar-nav")?.classList.add("is-collapsed");
      document.body.classList.add("sidebar-collapsed");
      document.getElementById("sidebar-collapse-btn")?.setAttribute("aria-pressed", "true");
    }
    restoreAutoDownloadRequests();
    failInterruptedAutoDownloadRequests();
    restoreActiveAutoDownloadToasts();
    switchMediaView("__INITIAL_MEDIA_VIEW__", { updateHistory: false });
    updatePageVisibilityState();
    renderGeneralSettings();
    renderSystemSummary();
    refresh();
    setInterval(() => {
      if (currentMediaView() === "discover") {
        setHeroCarouselIndex((window.__heroCarouselIndex || 0) + 1);
      }
    }, 6500);
    setInterval(refresh, 1500);
  </script>
</body>
</html>
"""
