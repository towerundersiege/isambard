from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


LOGGER = logging.getLogger("isambard.youtube")


@dataclass
class YouTubeEntry:
    id: str
    title: str
    url: str
    channel_id: str
    channel_title: str
    upload_date: str
    status: str = ""
    duration: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class YouTubeLookup:
    cache_key: str
    source_url: str
    source_title: str
    source_kind: str
    entries: list[YouTubeEntry]
    looked_up_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_key": self.cache_key,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "source_kind": self.source_kind,
            "entries": [entry.to_dict() for entry in self.entries],
            "looked_up_at": self.looked_up_at,
        }


@dataclass
class YouTubeSubscription:
    id: str
    source_url: str
    source_title: str
    source_kind: str
    known_video_ids: list[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    last_checked_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class YouTubeManager:
    def __init__(
        self,
        downloads_dir: Path,
        queue_video: Callable[[dict[str, Any]], Any],
        video_status: Callable[[str], str],
        require_outbound: Callable[[str], None] | None = None,
    ) -> None:
        self.downloads_dir = downloads_dir
        self.cache_file = downloads_dir / ".youtube-cache.json"
        self.subscriptions_file = downloads_dir / ".youtube-subscriptions.json"
        self.youtube_cookies_file = downloads_dir / "youtube" / "cookies.txt"
        self.lookup_cache: dict[str, dict[str, Any]] = {}
        self.subscriptions: list[YouTubeSubscription] = []
        self.latest_lookup_key: str = ""
        self._lock = threading.RLock()
        self._queue_video = queue_video
        self._video_status = video_status
        self._require_outbound = require_outbound or (lambda _context: None)
        self._load_state()
        poll_seconds = max(300, int(os.environ.get("YOUTUBE_SUBSCRIPTION_POLL_SECONDS", "1800")))
        self._poll_seconds = poll_seconds
        self._poller = threading.Thread(target=self._poll_loop, daemon=True, name="youtube-subscriptions")
        self._poller.start()

    def state(self) -> dict[str, Any]:
        with self._lock:
            latest_lookup = self.lookup_cache.get(self.latest_lookup_key) if self.latest_lookup_key else None
            subscriptions = [subscription.to_dict() for subscription in self.subscriptions]
        return {
            "latest_lookup": self._hydrate_lookup(latest_lookup).to_dict() if latest_lookup else None,
            "subscriptions": subscriptions,
        }

    def lookup(self, url: str, refresh: bool = False) -> YouTubeLookup:
        self._require_outbound("YouTube lookup")
        cache_key = self._cache_key(url)
        with self._lock:
            if not refresh and cache_key in self.lookup_cache:
                self.latest_lookup_key = cache_key
                self._save_state_locked()
                return self._hydrate_lookup(self.lookup_cache[cache_key])

        payload = self._extract(url)
        lookup = self._normalize_lookup(url, payload)
        with self._lock:
            self.lookup_cache[cache_key] = lookup.to_dict()
            self.latest_lookup_key = cache_key
            self._save_state_locked()
        LOGGER.info("youtube lookup source=%s entries=%s", url, len(lookup.entries))
        return lookup

    def queue_selected(self, cache_key: str, video_ids: list[str]) -> list[dict[str, Any]]:
        self._require_outbound("YouTube queueing")
        with self._lock:
            raw_lookup = self.lookup_cache.get(cache_key)
        if raw_lookup is None:
            raise ValueError("lookup not found")
        lookup = self._hydrate_lookup(raw_lookup)
        entries = [entry for entry in lookup.entries if entry.id in set(video_ids)]
        queued: list[dict[str, Any]] = []
        for entry in entries:
            if self._video_status(entry.id) == "downloaded":
                continue
            task = self._queue_video(
                {
                    "youtube_id": entry.id,
                    "title": entry.title,
                    "url": entry.url,
                    "channel_id": entry.channel_id,
                    "channel_title": entry.channel_title,
                    "upload_date": entry.upload_date,
                }
            )
            queued.append(task.to_dict())
        return queued

    def subscribe(self, cache_key: str) -> dict[str, Any]:
        self._require_outbound("YouTube subscriptions")
        with self._lock:
            raw_lookup = self.lookup_cache.get(cache_key)
            if raw_lookup is None:
                raise ValueError("lookup not found")
            lookup = self._hydrate_lookup(raw_lookup)
            for subscription in self.subscriptions:
                if subscription.source_url == lookup.source_url:
                    return subscription.to_dict()
            subscription = YouTubeSubscription(
                id=str(uuid.uuid4()),
                source_url=lookup.source_url,
                source_title=lookup.source_title,
                source_kind=lookup.source_kind,
                known_video_ids=[entry.id for entry in lookup.entries],
                last_checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self.subscriptions.append(subscription)
            self._save_state_locked()
        LOGGER.info("created youtube subscription source=%s", lookup.source_url)
        return subscription.to_dict()

    def refresh_subscription(self, subscription_id: str) -> dict[str, Any]:
        self._require_outbound("YouTube subscription refresh")
        with self._lock:
            subscription = next((item for item in self.subscriptions if item.id == subscription_id), None)
        if subscription is None:
            raise ValueError("subscription not found")
        new_count = self._refresh_subscription(subscription)
        return {"ok": True, "new_videos": new_count}

    def remove_subscription(self, subscription_id: str) -> bool:
        with self._lock:
            before = len(self.subscriptions)
            self.subscriptions = [item for item in self.subscriptions if item.id != subscription_id]
            changed = len(self.subscriptions) != before
            if changed:
                self._save_state_locked()
        if changed:
            LOGGER.info("removed youtube subscription id=%s", subscription_id)
        return changed

    def _poll_loop(self) -> None:
        while True:
            time.sleep(self._poll_seconds)
            with self._lock:
                subscriptions = list(self.subscriptions)
            try:
                self._require_outbound("YouTube subscription polling")
            except RuntimeError as exc:
                LOGGER.warning("%s", exc)
                continue
            for subscription in subscriptions:
                try:
                    self._refresh_subscription(subscription)
                except Exception:
                    LOGGER.exception("subscription refresh failed id=%s", subscription.id)

    def _refresh_subscription(self, subscription: YouTubeSubscription) -> int:
        lookup = self.lookup(subscription.source_url, refresh=True)
        known = set(subscription.known_video_ids)
        new_entries = [entry for entry in reversed(lookup.entries) if entry.id and entry.id not in known]
        queued = 0
        for entry in new_entries:
            if self._video_status(entry.id) == "downloaded":
                known.add(entry.id)
                continue
            self._queue_video(
                {
                    "youtube_id": entry.id,
                    "title": entry.title,
                    "url": entry.url,
                    "channel_id": entry.channel_id,
                    "channel_title": entry.channel_title,
                    "upload_date": entry.upload_date,
                }
            )
            known.add(entry.id)
            queued += 1
        with self._lock:
            subscription.known_video_ids = list(known)
            subscription.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            subscription.last_checked_at = subscription.updated_at
            self._save_state_locked()
        if queued:
            LOGGER.info("subscription queued new videos id=%s count=%s", subscription.id, queued)
        return queued

    def _extract(self, url: str) -> dict[str, Any]:
        yt_dlp_bin = os.environ.get("YT_DLP_BIN", "yt-dlp")
        resolved = shutil.which(yt_dlp_bin)
        if not resolved:
            raise RuntimeError(f"Unable to find yt-dlp binary: {yt_dlp_bin}")
        command = [resolved]
        js_runtime = self._detect_js_runtime()
        if js_runtime:
            command.extend(["--js-runtimes", js_runtime])
        if self.youtube_cookies_file.exists():
            command.extend(["--cookies", str(self.youtube_cookies_file)])
        lookup_limit = os.environ.get("YOUTUBE_LOOKUP_LIMIT", "").strip()
        if lookup_limit:
            command.extend(["--playlist-end", lookup_limit])
        command.extend([
            "--dump-single-json",
            "--flat-playlist",
            url,
        ])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "youtube lookup failed").strip()
            raise RuntimeError(message)
        return json.loads(result.stdout)

    def _normalize_lookup(self, source_url: str, payload: dict[str, Any]) -> YouTubeLookup:
        entries_raw = payload.get("entries") or [payload]
        source_title = str(payload.get("title") or payload.get("channel") or payload.get("uploader") or source_url)
        source_kind = str(payload.get("_type") or ("video" if len(entries_raw) == 1 else "collection"))
        entries: list[YouTubeEntry] = []
        for entry_raw in entries_raw:
            if not isinstance(entry_raw, dict):
                continue
            video_id = str(entry_raw.get("id") or "")
            title = str(entry_raw.get("title") or "Untitled")
            channel_id = str(
                entry_raw.get("channel_id")
                or entry_raw.get("uploader_id")
                or payload.get("channel_id")
                or payload.get("uploader_id")
                or "unknown-channel"
            )
            channel_title = str(
                entry_raw.get("channel")
                or entry_raw.get("uploader")
                or payload.get("channel")
                or payload.get("uploader")
                or channel_id
            )
            upload_date = str(entry_raw.get("upload_date") or payload.get("upload_date") or "")
            video_url = str(entry_raw.get("url") or "")
            if not video_url.startswith("http"):
                video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else source_url
            entries.append(
                YouTubeEntry(
                    id=video_id,
                    title=title,
                    url=video_url,
                    channel_id=channel_id,
                    channel_title=channel_title,
                    upload_date=upload_date,
                    duration=self._coerce_int(entry_raw.get("duration")),
                    status=self._video_status(video_id),
                )
            )
        cache_key = self._cache_key(source_url)
        return YouTubeLookup(
            cache_key=cache_key,
            source_url=source_url,
            source_title=source_title,
            source_kind=source_kind,
            entries=entries,
        )

    def _hydrate_lookup(self, raw_lookup: dict[str, Any]) -> YouTubeLookup:
        entries = [
            YouTubeEntry(
                **{
                    **entry,
                    "status": self._video_status(str(entry.get("id") or "")),
                }
            )
            for entry in raw_lookup.get("entries", [])
            if isinstance(entry, dict)
        ]
        return YouTubeLookup(
            cache_key=str(raw_lookup.get("cache_key") or ""),
            source_url=str(raw_lookup.get("source_url") or ""),
            source_title=str(raw_lookup.get("source_title") or ""),
            source_kind=str(raw_lookup.get("source_kind") or ""),
            entries=entries,
            looked_up_at=str(raw_lookup.get("looked_up_at") or ""),
        )

    def _cache_key(self, url: str) -> str:
        return hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()

    def _coerce_int(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _detect_js_runtime(self) -> str:
        configured = os.environ.get("YOUTUBE_JS_RUNTIME", "").strip()
        if configured:
            return configured
        for runtime in ("node", "bun", "deno"):
            if shutil.which(runtime):
                return runtime
        return ""

    def _load_state(self) -> None:
        try:
            if self.cache_file.exists():
                payload = json.loads(self.cache_file.read_text())
                if isinstance(payload, dict):
                    self.lookup_cache = {
                        str(key): value for key, value in payload.get("lookups", {}).items() if isinstance(value, dict)
                    }
                    self.latest_lookup_key = str(payload.get("latest_lookup_key") or "")
            if self.subscriptions_file.exists():
                payload = json.loads(self.subscriptions_file.read_text())
                if isinstance(payload, list):
                    self.subscriptions = [
                        YouTubeSubscription(**item) for item in payload if isinstance(item, dict)
                    ]
        except Exception:
            LOGGER.exception("failed to load youtube state")

    def _save_state_locked(self) -> None:
        cache_tmp = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
        cache_tmp.write_text(
            json.dumps(
                {
                    "lookups": self.lookup_cache,
                    "latest_lookup_key": self.latest_lookup_key,
                },
                indent=2,
            )
        )
        cache_tmp.replace(self.cache_file)

        subscriptions_tmp = self.subscriptions_file.with_suffix(self.subscriptions_file.suffix + ".tmp")
        subscriptions_tmp.write_text(json.dumps([item.to_dict() for item in self.subscriptions], indent=2))
        subscriptions_tmp.replace(self.subscriptions_file)
