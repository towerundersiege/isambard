from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .music_manager import MusicManager
from .music_models import (
    MusicMetadataFetchRequest,
    MusicQueueAddRequest,
    MusicQueueStartRequest,
    MusicSettingsPayload,
)


def install_music(app: FastAPI, music_manager: MusicManager) -> None:
    @app.get("/api/music/health")
    async def music_health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/music/settings")
    async def get_music_settings() -> dict[str, object]:
        return music_manager.get_settings()

    @app.post("/api/music/settings")
    async def update_music_settings(payload: MusicSettingsPayload) -> dict[str, object]:
        return music_manager.save_settings(payload.values)

    @app.post("/api/music/metadata/fetch")
    async def fetch_music_metadata(payload: MusicMetadataFetchRequest) -> dict[str, object]:
        try:
            summary = await music_manager.fetch_metadata(payload.url)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return summary.model_dump()

    @app.get("/api/music/history/fetch")
    async def music_fetch_history() -> list[dict[str, object]]:
        return music_manager.list_fetch_history()

    @app.delete("/api/music/history/fetch")
    async def clear_music_fetch_history() -> dict[str, bool]:
        music_manager.clear_fetch_history()
        return {"success": True}

    @app.get("/api/music/history/downloads")
    async def music_download_history() -> list[dict[str, object]]:
        return music_manager.list_download_history()

    @app.delete("/api/music/history/downloads")
    async def clear_music_download_history() -> dict[str, bool]:
        music_manager.clear_download_history()
        return {"success": True}

    @app.get("/api/music/queue")
    async def music_queue() -> dict[str, object]:
        return await music_manager.queue_summary()

    @app.post("/api/music/queue")
    async def add_music_queue(payload: MusicQueueAddRequest) -> dict[str, object]:
        return await music_manager.add_queue_item(payload)

    @app.post("/api/music/queue/start")
    async def start_music_queue(payload: MusicQueueStartRequest) -> dict[str, object]:
        try:
            return await music_manager.start_queue_item(payload.item_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.delete("/api/music/queue")
    async def clear_music_queue() -> dict[str, bool]:
        await music_manager.clear_queue()
        return {"success": True}
