from __future__ import annotations

import logging
import os
import uvicorn
from pathlib import Path

from downloader_app.download_manager import DownloadManager
from downloader_app.media_catalog import MediaCatalog
from downloader_app.music_manager import MusicManager
from downloader_app.vpn import MullvadGuard
from downloader_app.youtube_manager import YouTubeManager
from downloader_app.web import build_app


BASE_DIR = Path(__file__).resolve().parent
MULLVAD_GUARD = MullvadGuard()
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOAD_MANAGER = DownloadManager(
    downloads_dir=DOWNLOADS_DIR,
    state_file=DOWNLOADS_DIR / ".task-history.json",
    outbound_ready=MULLVAD_GUARD.is_connected,
)
YOUTUBE_MANAGER = YouTubeManager(
    downloads_dir=DOWNLOADS_DIR,
    queue_video=DOWNLOAD_MANAGER.enqueue_youtube,
    video_status=DOWNLOAD_MANAGER.youtube_video_status,
    require_outbound=MULLVAD_GUARD.assert_connected,
)
MUSIC_MANAGER = MusicManager(
    data_dir=DOWNLOADS_DIR / ".music",
    require_outbound=MULLVAD_GUARD.assert_connected,
)
MEDIA_CATALOG = MediaCatalog(
    jellyfin_url=os.environ.get("JELLYFIN_URL", ""),
    tmdb_api_key=os.environ.get("TMDB_API_KEY", ""),
    require_outbound=MULLVAD_GUARD.assert_connected,
    cache_dir=DOWNLOADS_DIR / ".media-cache",
)
app = build_app(DOWNLOAD_MANAGER, YOUTUBE_MANAGER, MUSIC_MANAGER, MULLVAD_GUARD, MEDIA_CATALOG)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("isambard").info("starting isambard app")
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
