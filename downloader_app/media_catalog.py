from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


LOGGER = logging.getLogger("isambard.media")
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"


class MediaCatalog:
    def __init__(
        self,
        jellyfin_url: str = "",
        tmdb_api_key: str = "",
        require_outbound: Callable[[str], None] | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.jellyfin_url = (jellyfin_url or "").rstrip("/")
        self.tmdb = TMDbClient(tmdb_api_key, require_outbound, cache_dir=cache_dir)

    def summary(self) -> dict[str, Any]:
        return {
            "jellyfin": {
                "configured": bool(self.jellyfin_url),
                "url": self.jellyfin_url,
            },
            "tmdb": self.tmdb.summary(),
        }

    def discover(self, query: str = "") -> dict[str, Any]:
        return self.tmdb.discover(query)

    def details(self, provider: str, provider_id: str, media_type: str) -> dict[str, Any]:
        return self.tmdb.details(provider_id, media_type)

    def jellyfin_web_url(self) -> str:
        return f"{self.jellyfin_url}/web/" if self.jellyfin_url else ""

    def jellyfin_search_url(self, query: str) -> str:
        if not self.jellyfin_url:
            return ""
        clean_query = (query or "").strip()
        if not clean_query:
            return self.jellyfin_web_url()
        return f"{self.jellyfin_url}/web/#/search.html?query={urllib.parse.quote(clean_query)}"

    def auto_find_payload(
        self,
        title: str,
        year: str = "",
        media_type: str = "movie",
        site: str = "yflix",
        season: int | None = None,
        episode: int | None = None,
        poster_url: str = "",
        backdrop_url: str = "",
    ) -> dict[str, Any]:
        normalized_media_type = (media_type or "movie").strip().lower()
        clean_title = title.strip()
        clean_year = year.strip()
        search_hint = " ".join(part for part in [clean_title, clean_year] if part).strip()
        if not search_hint:
            search_hint = "Search title"
        if site == "yflix":
            query = {
                "keyword": clean_title or search_hint,
                "isambard_title": clean_title,
                "isambard_year": clean_year,
                "isambard_media_type": normalized_media_type,
            }
            if season:
                query["isambard_season"] = str(season)
            if episode:
                query["isambard_episode"] = str(episode)
            if poster_url:
                query["isambard_poster_url"] = poster_url
            if backdrop_url:
                query["isambard_backdrop_url"] = backdrop_url
            target = "https://yflix.to/browser?" + urllib.parse.urlencode(query)
        else:
            target = "https://dashflix.top/"
        return {
            "ok": True,
            "site": site,
            "search_hint": search_hint,
            "target_url": target,
            "media_type": normalized_media_type,
            "season": season,
            "episode": episode,
            "poster_url": poster_url,
            "backdrop_url": backdrop_url,
        }


class _CachedClient:
    def __init__(self, ttl_seconds: int = 600) -> None:
        self._ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _cached(self, key: str, loader: Callable[[], Any], ttl_seconds: int | None = None) -> Any:
        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl_seconds
        with self._lock:
            entry = self._cache.get(key)
            if entry and (now - entry[0]) < ttl:
                return entry[1]
        value = loader()
        with self._lock:
            self._cache[key] = (now, value)
        return value


class TMDbClient(_CachedClient):
    def __init__(
        self,
        api_key: str = "",
        require_outbound: Callable[[str], None] | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        super().__init__(ttl_seconds=3600)
        self.api_key = (api_key or "").strip()
        self.require_outbound = require_outbound or (lambda _context: None)
        self.cache_dir = cache_dir

    def configured(self) -> bool:
        return bool(self.api_key)

    def summary(self) -> dict[str, Any]:
        return {"configured": self.configured()}

    def discover(self, query: str = "") -> dict[str, Any]:
        query = (query or "").strip()
        if not self.configured():
            return {
                "configured": False,
                "query": query,
                "sections": [],
                "source": "tmdb",
            }

        def load() -> dict[str, Any]:
            self.require_outbound("TMDb discovery")
            if query:
                movie_results = self._request_json(
                    "/search/movie",
                    {"query": query, "include_adult": "false", "page": "1"},
                )
                tv_results = self._request_json(
                    "/search/tv",
                    {"query": query, "include_adult": "false", "page": "1"},
                )
                return {
                    "configured": True,
                    "query": query,
                    "source": "tmdb",
                    "sections": [
                        self._section("search_movies", f'Search Movies for "{query}"', "movie", movie_results),
                        self._section("search_tv", f'Search TV for "{query}"', "tv", tv_results),
                    ],
                }

            trending_movies = self._request_json("/trending/movie/week", {"page": "1"})
            trending_tv = self._request_json("/trending/tv/week", {"page": "1"})
            popular_movies = self._request_json("/movie/popular", {"page": "1"})
            airing_today = self._request_json("/tv/airing_today", {"page": "1"})
            return {
                "configured": True,
                "query": "",
                "source": "tmdb",
                "sections": [
                    self._section("trending_movies", "Trending Movies", "movie", trending_movies),
                    self._section("trending_tv", "Trending TV Shows", "tv", trending_tv),
                    self._section("popular_movies", "Popular Movies", "movie", popular_movies),
                    self._section("airing_today", "Airing Today", "tv", airing_today),
                ],
            }

        if not query:
            return self._daily_cached("discover", load)
        ttl = 3600
        return self._cached(f"discover:{query}", load, ttl_seconds=ttl)

    def _daily_cached(self, key: str, loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        memory_key = f"{key}:{today}"
        return self._cached(memory_key, lambda: self._daily_cached_uncached(key, today, loader), ttl_seconds=86400)

    def _daily_cached_uncached(self, key: str, today: str, loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        cache_file = self._daily_cache_file(key, today)
        if cache_file and cache_file.is_file():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                LOGGER.warning("ignoring unreadable TMDb cache file path=%s", cache_file)
        try:
            payload = loader()
        except RuntimeError:
            fallback = self._latest_daily_cache_file(key)
            if fallback:
                try:
                    LOGGER.info("using stale TMDb cache file path=%s", fallback)
                    return json.loads(fallback.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    LOGGER.warning("ignoring unreadable stale TMDb cache file path=%s", fallback)
            raise
        if cache_file:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(payload), encoding="utf-8")
            except OSError:
                LOGGER.warning("failed to write TMDb cache file path=%s", cache_file)
        return payload

    def _daily_cache_file(self, key: str, day: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"tmdb-{key}-{day}.json"

    def _latest_daily_cache_file(self, key: str) -> Path | None:
        if not self.cache_dir or not self.cache_dir.is_dir():
            return None
        matches = sorted(self.cache_dir.glob(f"tmdb-{key}-*.json"), reverse=True)
        return matches[0] if matches else None

    def details(self, provider_id: str, media_type: str) -> dict[str, Any]:
        provider_id = (provider_id or "").strip()
        media_type = (media_type or "movie").strip().lower()
        if not self.configured():
            return {"configured": False, "provider": "tmdb", "media_type": media_type, "seasons": []}
        if not provider_id:
            return {"configured": True, "provider": "tmdb", "media_type": media_type, "seasons": []}

        def load() -> dict[str, Any]:
            self.require_outbound("TMDb details")
            if media_type == "tv":
                show = self._request_json(f"/tv/{provider_id}", {})
                seasons = []
                title = str(show.get("name") or show.get("original_name") or "").strip()
                year = str(show.get("first_air_date") or "")[:4]
                for season in show.get("seasons", []):
                    season_number = int(season.get("season_number") or 0)
                    if season_number <= 0:
                        continue
                    season_payload = self._request_json(f"/tv/{provider_id}/season/{season_number}", {})
                    episodes = []
                    for episode in season_payload.get("episodes", []):
                        episode_number = int(episode.get("episode_number") or 0)
                        if episode_number <= 0:
                            continue
                        code = f"S{season_number:02d}E{episode_number:02d}"
                        episodes.append(
                            {
                                "id": f"tmdb-tv-{provider_id}-s{season_number:02d}e{episode_number:02d}",
                                "title": str(episode.get("name") or code),
                                "episode_number": episode_number,
                                "season_number": season_number,
                                "air_date": str(episode.get("air_date") or ""),
                                "search_hint": " ".join(part for part in [title, year, code] if part).strip(),
                            }
                        )
                    seasons.append(
                        {
                            "id": f"tmdb-tv-{provider_id}-season-{season_number}",
                            "title": str(season.get("name") or f"Season {season_number}"),
                            "season_number": season_number,
                            "episode_count": len(episodes),
                            "search_hint": " ".join(part for part in [title, year, f"Season {season_number}"] if part).strip(),
                            "episodes": episodes,
                        }
                    )
                return {
                    "configured": True,
                    "provider": "tmdb",
                    "provider_id": provider_id,
                    "media_type": "tv",
                    "title": title,
                    "year": year,
                    "seasons": seasons,
                }
            movie = self._request_json(f"/movie/{provider_id}", {})
            return {
                "configured": True,
                "provider": "tmdb",
                "provider_id": provider_id,
                "media_type": "movie",
                "title": str(movie.get("title") or movie.get("original_title") or ""),
                "year": str(movie.get("release_date") or "")[:4],
                "seasons": [],
            }

        return self._cached(f"details:{media_type}:{provider_id}", load, ttl_seconds=86400)

    def _request_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        query = urllib.parse.urlencode({"api_key": self.api_key, **params})
        url = f"https://api.themoviedb.org/3{path}?{query}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            LOGGER.warning("tmdb request failed status=%s path=%s detail=%s", exc.code, path, detail[:240])
            raise RuntimeError(f"TMDb request failed with {exc.code}") from exc
        except urllib.error.URLError as exc:
            LOGGER.warning("tmdb request failed path=%s error=%s", path, exc)
            raise RuntimeError("TMDb request failed") from exc

    def _section(
        self,
        section_id: str,
        title: str,
        media_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        items = [self._map_item(item, media_type) for item in payload.get("results", [])[:12]]
        return {"id": section_id, "title": title, "items": [item for item in items if item]}

    def _map_item(self, item: dict[str, Any], media_type: str) -> dict[str, Any] | None:
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            return None
        year = ""
        date_value = str(item.get("release_date") or item.get("first_air_date") or "").strip()
        if len(date_value) >= 4:
            year = date_value[:4]
        poster_path = str(item.get("poster_path") or "")
        backdrop_path = str(item.get("backdrop_path") or "")
        return {
            "id": f"tmdb-{media_type}-{item.get('id')}",
            "provider": "tmdb",
            "provider_id": str(item.get("id") or ""),
            "title": title,
            "year": year,
            "media_type": media_type,
            "overview": str(item.get("overview") or ""),
            "rating": float(item.get("vote_average") or 0.0),
            "poster_url": self._image_url(poster_path, "w342"),
            "backdrop_url": self._image_url(backdrop_path, "w780"),
            "search_hint": " ".join(part for part in [title, year] if part).strip(),
            "actions": {
                "auto_find": True,
                "open_jellyfin": False,
            },
        }

    def _image_url(self, path: str, size: str) -> str:
        if not path:
            return ""
        return f"{TMDB_IMAGE_BASE}/{size}{path}"
