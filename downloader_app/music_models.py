from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MusicSettingsPayload(BaseModel):
    values: dict[str, Any]


class MusicMetadataFetchRequest(BaseModel):
    url: str


class MusicQueueAddRequest(BaseModel):
    spotify_id: str = ""
    track_name: str
    artist_name: str
    album_name: str = ""


class MusicQueueStartRequest(BaseModel):
    item_id: str


class MusicQueueItem(BaseModel):
    id: str
    spotify_id: str = ""
    track_name: str
    artist_name: str
    album_name: str = ""
    status: Literal["queued", "downloading", "completed", "failed", "skipped"] = "queued"
    progress: float = 0.0
    speed: float = 0.0
    error_message: str = ""
    file_path: str = ""
    start_time: int = 0
    end_time: int = 0


class MusicFetchHistoryItem(BaseModel):
    id: str
    url: str
    type: str
    name: str
    info: str
    image: str = ""
    data: str
    timestamp: int


class MusicDownloadHistoryItem(BaseModel):
    id: str
    spotify_id: str = ""
    title: str
    artists: str
    album: str = ""
    duration_str: str = ""
    cover_url: str = ""
    quality: str = ""
    format: str = ""
    path: str = ""
    source: str = "isambard-music"
    timestamp: int


class MusicMetadataSummary(BaseModel):
    entity_type: str
    title: str
    subtitle: str = ""
    image: str = ""
    track_count: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
