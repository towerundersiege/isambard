from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import uuid
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .metadata_resolver import MetadataResolver


PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%(?:\s+of\s+(?P<size>\S+))?"
    r"(?:\s+at\s+(?P<speed>\S+))?(?:\s+ETA\s+(?P<eta>\S+))?"
)
LOGGER = logging.getLogger("isambard.downloads")


def _read_concurrency(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


@dataclass
class DownloadTask:
    id: str
    title: str
    url: str
    output_template: str
    media_type: str = "movie"
    series_name: str = ""
    series_year: str = ""
    season: int | None = None
    episode: int | None = None
    status: str = "queued"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    started_at: str | None = None
    finished_at: str | None = None
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    filesize: str = ""
    output: str = ""
    error: str = ""
    source_type: str = "standard"
    youtube_id: str = ""
    channel_id: str = ""
    upload_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DownloadTask":
        return cls(**payload)


class DownloadManager:
    def __init__(self, downloads_dir: Path, state_file: Path | None = None) -> None:
        self.downloads_dir = downloads_dir
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = state_file or (self.downloads_dir / ".task-history.json")
        self.youtube_archive_file = self.downloads_dir / ".youtube-archive.txt"
        self.youtube_cookies_file = self.downloads_dir / "youtube" / "cookies.txt"
        self._resolver = MetadataResolver()
        self._lock = threading.RLock()
        self._tasks: list[DownloadTask] = []
        self._index: dict[str, DownloadTask] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._stop_requested: set[str] = set()
        self._condition = threading.Condition(self._lock)
        self._running_counts: dict[str, int] = {}
        self._concurrency_limits = {
            "movie": _read_concurrency("MOVIE_DOWNLOAD_CONCURRENCY", 5),
            "tv": _read_concurrency("TV_DOWNLOAD_CONCURRENCY", 5),
            "youtube": _read_concurrency("YOUTUBE_DOWNLOAD_CONCURRENCY", 5),
            "standard": _read_concurrency("STANDARD_DOWNLOAD_CONCURRENCY", 5),
        }
        self._load_state()
        worker_count = max(1, sum(self._concurrency_limits.values()))
        self._workers = [
            threading.Thread(target=self._run, daemon=True, name=f"download-worker-{index + 1}")
            for index in range(worker_count)
        ]
        for worker in self._workers:
            worker.start()

    def enqueue(self, title: str, url: str, metadata: dict[str, Any] | None = None) -> DownloadTask:
        if metadata and metadata.get("source_type") == "youtube":
            return self.enqueue_youtube(metadata)
        resolved = self._resolver.resolve(title, metadata)
        task = DownloadTask(
            id=str(uuid.uuid4()),
            title=resolved.display_title,
            url=url,
            output_template=str(self.downloads_dir / resolved.output_template),
            media_type=resolved.media_type,
            source_type=resolved.media_type,
            series_name=resolved.series_name,
            series_year=resolved.series_year,
            season=resolved.season,
            episode=resolved.episode,
        )
        with self._lock:
            self._tasks.append(task)
            self._index[task.id] = task
            self._save_state_locked()
            self._condition.notify_all()
        LOGGER.info("enqueued task id=%s title=%s url=%s", task.id, task.title, task.url)
        return task

    def enqueue_youtube(self, metadata: dict[str, Any]) -> DownloadTask:
        resolved = self._resolver.resolve_youtube(metadata)
        task = DownloadTask(
            id=str(uuid.uuid4()),
            title=resolved.display_title,
            url=str(metadata.get("url") or ""),
            output_template=str(self.downloads_dir / resolved.output_template),
            media_type=resolved.media_type,
            source_type=resolved.media_type,
            youtube_id=resolved.youtube_id,
            channel_id=resolved.channel_id,
            upload_date=resolved.upload_date,
        )
        with self._lock:
            self._tasks.append(task)
            self._index[task.id] = task
            self._save_state_locked()
            self._condition.notify_all()
        LOGGER.info(
            "enqueued youtube task id=%s title=%s video_id=%s",
            task.id,
            task.title,
            task.youtube_id,
        )
        return task

    def youtube_video_status(self, youtube_id: str) -> str:
        youtube_id = (youtube_id or "").strip()
        if not youtube_id:
            return ""
        with self._lock:
            for task in reversed(self._tasks):
                if task.youtube_id != youtube_id:
                    continue
                if task.status == "completed":
                    return "downloaded"
                return task.status
        if self.youtube_archive_file.exists():
            needle = f"youtube {youtube_id}"
            try:
                for line in self.youtube_archive_file.read_text().splitlines():
                    if line.strip() == needle:
                        return "downloaded"
            except Exception:
                LOGGER.exception("failed to read youtube archive")
        return ""

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            running = [t.to_dict() for t in self._tasks if t.status == "running"]
            queued = [t.to_dict() for t in self._tasks if t.status == "queued"]
            completed = [
                t.to_dict()
                for t in self._tasks
                if t.status in {"completed", "failed", "stopped"}
            ]
        completed.sort(key=lambda task: task.get("finished_at") or task.get("created_at") or "", reverse=True)
        return {"running": running, "queued": queued, "completed": completed}

    def stop_task(self, task_id: str) -> DownloadTask | None:
        with self._lock:
            task = self._index.get(task_id)
            if task is None:
                return None
            if task.status == "queued":
                task.status = "stopped"
                task.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                task.error = "Stopped before download started"
                self._delete_generated_files(task)
                self._save_state_locked()
                self._condition.notify_all()
                LOGGER.info("removed queued task id=%s title=%s", task.id, task.title)
                return task
            if task.status != "running":
                return task
            self._stop_requested.add(task_id)
            process = self._processes.get(task_id)
            LOGGER.info("stop requested for running task id=%s title=%s", task.id, task.title)

        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        with self._lock:
            task = self._index.get(task_id)
            if task is not None and task.status == "running":
                task.status = "stopped"
                task.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                task.error = "Stopped by user"
                self._delete_generated_files(task)
                self._save_state_locked()
                LOGGER.info("stopped running task id=%s title=%s", task.id, task.title)
            return task

    def _run(self) -> None:
        while True:
            with self._condition:
                task = self._reserve_next_task_locked()
                while task is None:
                    self._condition.wait()
                    task = self._reserve_next_task_locked()
            self._execute(task)

    def _execute(self, task: DownloadTask) -> None:
        yt_dlp_bin = os.environ.get("YT_DLP_BIN", "yt-dlp")
        resolved = shutil.which(yt_dlp_bin)
        if not resolved:
            with self._lock:
                task.status = "failed"
                task.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                task.error = f"Unable to find yt-dlp binary: {yt_dlp_bin}"
                self._save_state_locked()
                self._release_slot_locked(task)
            LOGGER.error("yt-dlp not found task id=%s binary=%s", task.id, yt_dlp_bin)
            return

        output_template = Path(task.output_template)
        output_template.parent.mkdir(parents=True, exist_ok=True)
        command = [
            resolved,
            "--newline",
            "--abort-on-unavailable-fragments",
            "--fragment-retries",
            "20",
            "--retries",
            "10",
            "--merge-output-format",
            "mp4",
            "-o",
            str(output_template),
            task.url,
        ]
        if task.source_type == "youtube":
            youtube_args = ["--download-archive", str(self.youtube_archive_file)]
            if self.youtube_cookies_file.exists():
                youtube_args.extend(["--cookies", str(self.youtube_cookies_file)])
            command[1:1] = youtube_args
        LOGGER.info("starting download task id=%s title=%s", task.id, task.title)

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            with self._lock:
                task.status = "failed"
                task.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                task.error = str(exc)
                self._save_state_locked()
                self._release_slot_locked(task)
            LOGGER.exception("failed to start yt-dlp task id=%s", task.id)
            return

        with self._lock:
            self._processes[task.id] = process

        assert process.stdout is not None
        output_lines: list[str] = []
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            output_lines.append(line)
            if len(output_lines) > 40:
                output_lines = output_lines[-40:]
            self._update_progress(task, line, "\n".join(output_lines))

        return_code = process.wait()
        with self._lock:
            self._processes.pop(task.id, None)
            stop_requested = task.id in self._stop_requested
            if stop_requested:
                self._stop_requested.discard(task.id)
        with self._lock:
            task.output = "\n".join(output_lines)
            task.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if stop_requested:
                task.status = "stopped"
                task.error = "Stopped by user"
                self._delete_generated_files(task)
                self._save_state_locked()
                LOGGER.info("download stopped task id=%s title=%s", task.id, task.title)
                self._apply_youtube_cooldown(task)
                with self._lock:
                    self._release_slot_locked(task)
                return
            if return_code != 0:
                task.status = "failed"
                task.error = task.output.splitlines()[-1] if task.output else "yt-dlp failed"
                self._save_state_locked()
                LOGGER.error("download failed task id=%s title=%s return_code=%s", task.id, task.title, return_code)
                self._apply_youtube_cooldown(task)
                with self._lock:
                    self._release_slot_locked(task)
                return

        verification_error = self._verify_download(task)
        with self._lock:
            task.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if verification_error:
                task.status = "failed"
                task.error = verification_error
                LOGGER.error("verification failed task id=%s title=%s error=%s", task.id, task.title, verification_error)
            else:
                task.status = "completed"
                task.progress = 100.0
                task.eta = ""
                LOGGER.info("download completed task id=%s title=%s", task.id, task.title)
            self._save_state_locked()
        self._apply_youtube_cooldown(task)
        with self._lock:
            self._release_slot_locked(task)

    def _update_progress(self, task: DownloadTask, line: str, output: str) -> None:
        match = PROGRESS_RE.search(line)
        with self._lock:
            task.output = output
            if not match:
                self._save_state_locked()
                return
            task.progress = float(match.group("percent"))
            size = match.group("size")
            task.filesize = size.lstrip("~") if size else task.filesize
            task.speed = match.group("speed") or task.speed
            task.eta = match.group("eta") or task.eta
            self._save_state_locked()

    def _verify_download(self, task: DownloadTask) -> str:
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "ffmpeg")
        resolved = shutil.which(ffmpeg_bin)
        if not resolved:
            return f"Unable to find ffmpeg binary for verification: {ffmpeg_bin}"

        output_path = Path(task.output_template.replace("%(ext)s", "mp4"))
        if not output_path.exists():
            return f"Downloaded file not found for verification: {output_path}"

        command = [
            resolved,
            "-v",
            "error",
            "-i",
            str(output_path),
            "-f",
            "null",
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:
            return f"ffmpeg verification failed to start: {exc}"

        verification_output = (result.stderr or "").strip()
        if verification_output:
            with self._lock:
                task.output = "\n".join(
                    [line for line in [task.output, "[verify] ffmpeg decode check failed", verification_output] if line]
                )
            return "ffmpeg verification detected decode errors"

        with self._lock:
            task.output = "\n".join(
                [line for line in [task.output, "[verify] ffmpeg decode check passed"] if line]
            )
        return ""

    def _delete_generated_files(self, task: DownloadTask) -> None:
        output_path = Path(task.output_template.replace("%(ext)s", "mp4"))
        candidates = [output_path]
        stem = output_path.stem
        candidates.extend(output_path.parent.glob(f"{stem}*"))
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text())
        except Exception:
            return
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, list):
            return
        for raw_task in tasks:
            if not isinstance(raw_task, dict):
                continue
            try:
                task = DownloadTask.from_dict(raw_task)
            except Exception:
                continue
            self._tasks.append(task)
            self._index[task.id] = task
            if task.status in {"queued", "running"}:
                task.status = "queued"
                task.finished_at = None
                task.error = ""
                task.speed = ""
                task.eta = ""
                LOGGER.info("requeued persisted task id=%s title=%s", task.id, task.title)
        with self._condition:
            self._condition.notify_all()

    def _save_state_locked(self) -> None:
        temp_path = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temp_path.write_text(json.dumps({"tasks": [task.to_dict() for task in self._tasks]}, indent=2))
        temp_path.replace(self.state_file)

    def _task_source(self, task: DownloadTask) -> str:
        return (task.source_type or task.media_type or "standard").strip() or "standard"

    def _limit_for_source(self, source: str) -> int:
        return self._concurrency_limits.get(source, self._concurrency_limits["standard"])

    def _reserve_next_task_locked(self) -> DownloadTask | None:
        for task in self._tasks:
            if task.status != "queued":
                continue
            source = self._task_source(task)
            if self._running_counts.get(source, 0) >= self._limit_for_source(source):
                continue
            self._running_counts[source] = self._running_counts.get(source, 0) + 1
            task.status = "running"
            task.started_at = task.started_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
            task.finished_at = None
            task.error = ""
            task.speed = ""
            task.eta = ""
            self._save_state_locked()
            return task
        return None

    def _release_slot_locked(self, task: DownloadTask) -> None:
        source = self._task_source(task)
        current = self._running_counts.get(source, 0)
        if current <= 1:
            self._running_counts.pop(source, None)
        else:
            self._running_counts[source] = current - 1
        self._condition.notify_all()

    def _apply_youtube_cooldown(self, task: DownloadTask) -> None:
        if task.source_type != "youtube":
            return
        cooldown = max(0, int(os.environ.get("YOUTUBE_DOWNLOAD_COOLDOWN_SECONDS", "20")))
        if cooldown:
            LOGGER.info("youtube cooldown task id=%s seconds=%s", task.id, cooldown)
            time.sleep(cooldown)
