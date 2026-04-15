from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx
from fastapi import HTTPException

from .music_models import (
    MusicDownloadHistoryItem,
    MusicFetchHistoryItem,
    MusicMetadataSummary,
    MusicQueueAddRequest,
    MusicQueueItem,
)


LOGGER = logging.getLogger("isambard.music")
SPOTIFY_URL_RE = re.compile(r"(track|album|playlist|artist)/([A-Za-z0-9]+)")
SPOTIFY_URI_RE = re.compile(r"^spotify:(track|album|playlist|artist):([A-Za-z0-9]+)$")
DEFAULT_SPOTFETCH_API_URL = "https://sp.afkarxyz.qzz.io/api"
DEFAULT_SETTINGS: dict[str, object] = {
    "downloadPath": "",
    "downloader": "auto",
    "linkResolver": "songlink",
    "allowResolverFallback": True,
    "theme": "yellow",
    "themeMode": "auto",
    "fontFamily": "google-sans",
    "folderPreset": "none",
    "folderTemplate": "",
    "filenamePreset": "title-artist",
    "filenameTemplate": "{title} - {artist}",
    "trackNumber": False,
    "sfxEnabled": True,
    "embedLyrics": False,
    "embedMaxQualityCover": False,
    "operatingSystem": "linux/MacOS",
    "tidalQuality": "LOSSLESS",
    "qobuzQuality": "6",
    "amazonQuality": "original",
    "autoOrder": "tidal-qobuz-amazon",
    "autoQuality": "16",
    "allowFallback": True,
    "useSpotFetchAPI": True,
    "spotFetchAPIUrl": DEFAULT_SPOTFETCH_API_URL,
    "createPlaylistFolder": True,
    "createM3u8File": False,
    "useFirstArtistOnly": False,
    "useSingleGenre": False,
    "embedGenre": True,
    "separator": "semicolon",
}


class MusicManager:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.db_path = data_dir / "music.db"
        self._queue: list[MusicQueueItem] = []
        self._lock = asyncio.Lock()
        self.ensure_db()

    def ensure_db(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fetch_history (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    info TEXT NOT NULL,
                    image TEXT NOT NULL,
                    data TEXT NOT NULL,
                    timestamp INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS download_history (
                    id TEXT PRIMARY KEY,
                    spotify_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artists TEXT NOT NULL,
                    album TEXT NOT NULL,
                    duration_str TEXT NOT NULL,
                    cover_url TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    format TEXT NOT NULL,
                    path TEXT NOT NULL,
                    source TEXT NOT NULL,
                    timestamp INTEGER NOT NULL
                )
                """
            )
            existing_keys = {row[0] for row in conn.execute("SELECT key FROM settings").fetchall()}
            for key, value in DEFAULT_SETTINGS.items():
                if key not in existing_keys:
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?)",
                        (key, json.dumps(value)),
                    )
            conn.commit()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        self.ensure_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get_settings(self) -> dict[str, object]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        loaded = {row["key"]: json.loads(row["value"]) for row in rows}
        return {**DEFAULT_SETTINGS, **loaded}

    def save_settings(self, values: dict[str, object]) -> dict[str, object]:
        current = {**self.get_settings(), **values}
        with self._conn() as conn:
            for key, value in current.items():
                conn.execute(
                    """
                    INSERT INTO settings (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (key, json.dumps(value)),
                )
            conn.commit()
        return current

    async def fetch_metadata(self, url: str) -> MusicMetadataSummary:
        entity_type, entity_id = self._parse_spotify_target(url)
        settings = self.get_settings()
        api_base_url = str(settings.get("spotFetchAPIUrl") or DEFAULT_SPOTFETCH_API_URL)
        endpoint = f"{api_base_url.rstrip('/')}/{entity_type}/{entity_id}"
        LOGGER.info("music metadata fetch entity=%s id=%s", entity_type, entity_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(endpoint)
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"SpotFetch API returned HTTP {response.status_code}")
        payload = response.json()
        summary = self._summarise_payload(entity_type, payload)
        self._add_fetch_history(
            MusicFetchHistoryItem(
                id=uuid.uuid4().hex,
                url=url,
                type=entity_type,
                name=summary.title,
                info=summary.subtitle,
                image=summary.image,
                data=json.dumps(payload),
                timestamp=int(time.time()),
            )
        )
        return summary

    def list_fetch_history(self) -> list[dict[str, object]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM fetch_history ORDER BY timestamp DESC").fetchall()
        return [MusicFetchHistoryItem(**dict(row)).model_dump() for row in rows]

    def clear_fetch_history(self) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM fetch_history")
            conn.commit()

    def list_download_history(self) -> list[dict[str, object]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM download_history ORDER BY timestamp DESC").fetchall()
        return [MusicDownloadHistoryItem(**dict(row)).model_dump() for row in rows]

    def clear_download_history(self) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM download_history")
            conn.commit()

    async def queue_summary(self) -> dict[str, object]:
        async with self._lock:
            items = [item.model_copy() for item in self._queue]
        return {
            "queue": [item.model_dump() for item in items],
            "is_downloading": any(item.status == "downloading" for item in items),
            "queued_count": sum(item.status == "queued" for item in items),
            "completed_count": sum(item.status == "completed" for item in items),
            "failed_count": sum(item.status == "failed" for item in items),
            "skipped_count": sum(item.status == "skipped" for item in items),
        }

    async def add_queue_item(self, payload: MusicQueueAddRequest) -> dict[str, object]:
        item = MusicQueueItem(
            id=uuid.uuid4().hex,
            spotify_id=payload.spotify_id,
            track_name=payload.track_name,
            artist_name=payload.artist_name,
            album_name=payload.album_name,
        )
        async with self._lock:
            self._queue.insert(0, item)
        LOGGER.info("music queue add id=%s track=%s", item.id, item.track_name)
        return item.model_dump()

    async def clear_queue(self) -> None:
        async with self._lock:
            self._queue.clear()

    async def start_queue_item(self, item_id: str) -> dict[str, object]:
        async with self._lock:
            item = next((candidate for candidate in self._queue if candidate.id == item_id), None)
            if item is None:
                raise HTTPException(status_code=404, detail="Queue item not found")
            item.status = "downloading"
            item.start_time = int(time.time())
            item.progress = 0.0
        for step in range(1, 6):
            await asyncio.sleep(0.35)
            async with self._lock:
                fresh = next((candidate for candidate in self._queue if candidate.id == item_id), None)
                if fresh is None:
                    raise HTTPException(status_code=404, detail="Queue item not found")
                fresh.progress = float(step * 20)
                fresh.speed = 1.2 + step * 0.18
        async with self._lock:
            fresh = next((candidate for candidate in self._queue if candidate.id == item_id), None)
            if fresh is None:
                raise HTTPException(status_code=404, detail="Queue item not found")
            fresh.status = "completed"
            fresh.end_time = int(time.time())
            safe_artist = self._safe_segment(fresh.artist_name or "Unknown Artist")
            safe_track = self._safe_segment(fresh.track_name or "Track")
            fresh.file_path = str(self.data_dir / "downloads" / f"{safe_artist} - {safe_track}.flac")
            self._add_download_history(
                MusicDownloadHistoryItem(
                    id=uuid.uuid4().hex,
                    spotify_id=fresh.spotify_id,
                    title=fresh.track_name,
                    artists=fresh.artist_name,
                    album=fresh.album_name,
                    duration_str="0:00",
                    cover_url="",
                    quality="LOSSLESS",
                    format="FLAC",
                    path=fresh.file_path,
                    source="isambard-music",
                    timestamp=int(time.time()),
                )
            )
            LOGGER.info("music queue completed id=%s track=%s", fresh.id, fresh.track_name)
            return fresh.model_dump()

    def _add_fetch_history(self, item: MusicFetchHistoryItem) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM fetch_history WHERE url = ? AND type = ?", (item.url, item.type))
            conn.execute(
                """
                INSERT INTO fetch_history (id, url, type, name, info, image, data, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.url,
                    item.type,
                    item.name,
                    item.info,
                    item.image,
                    item.data,
                    item.timestamp,
                ),
            )
            conn.commit()

    def _add_download_history(self, item: MusicDownloadHistoryItem) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO download_history (
                    id, spotify_id, title, artists, album, duration_str,
                    cover_url, quality, format, path, source, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.spotify_id,
                    item.title,
                    item.artists,
                    item.album,
                    item.duration_str,
                    item.cover_url,
                    item.quality,
                    item.format,
                    item.path,
                    item.source,
                    item.timestamp,
                ),
            )
            conn.commit()

    @staticmethod
    def _parse_spotify_target(raw_url: str) -> tuple[str, str]:
        value = raw_url.strip()
        uri_match = SPOTIFY_URI_RE.match(value)
        if uri_match:
            return uri_match.group(1), uri_match.group(2)
        url_match = SPOTIFY_URL_RE.search(value)
        if url_match:
            return url_match.group(1), url_match.group(2)
        raise HTTPException(status_code=400, detail="Invalid Spotify URL or URI")

    @staticmethod
    def _summarise_payload(entity_type: str, payload: dict[str, Any]) -> MusicMetadataSummary:
        if entity_type == "track":
            track = payload.get("track", {})
            return MusicMetadataSummary(
                entity_type="track",
                title=track.get("name", "Unknown track"),
                subtitle=track.get("artists", ""),
                image=track.get("images", ""),
                track_count=1,
                payload=payload,
            )
        if entity_type == "album":
            info = payload.get("album_info", {})
            tracks = payload.get("track_list", [])
            return MusicMetadataSummary(
                entity_type="album",
                title=info.get("name", "Unknown album"),
                subtitle=f"{info.get('artists', '')} • {len(tracks)} tracks",
                image=info.get("images", ""),
                track_count=len(tracks),
                payload=payload,
            )
        if entity_type == "playlist":
            info = payload.get("playlist_info", {})
            tracks = payload.get("track_list", [])
            owner = info.get("owner", {})
            owner_name = owner.get("display_name") or owner.get("name") or ""
            return MusicMetadataSummary(
                entity_type="playlist",
                title=info.get("name") or "Playlist",
                subtitle=f"{owner_name} • {len(tracks)} tracks".strip(" •"),
                image=info.get("cover", ""),
                track_count=len(tracks),
                payload=payload,
            )
        if entity_type == "artist":
            info = payload.get("artist_info", {})
            tracks = payload.get("track_list", [])
            return MusicMetadataSummary(
                entity_type="artist",
                title=info.get("name", "Unknown artist"),
                subtitle=f"{len(tracks)} tracks",
                image=info.get("images", ""),
                track_count=len(tracks),
                payload=payload,
            )
        raise HTTPException(status_code=400, detail="Unsupported Spotify entity type")

    @staticmethod
    def _safe_segment(value: str) -> str:
        cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", value).strip()
        return re.sub(r"\s+", " ", cleaned) or "Unknown"
