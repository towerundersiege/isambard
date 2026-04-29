from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


LOGGER = logging.getLogger("isambard.media")
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"


class MediaCatalog:
    def __init__(
        self,
        jellyfin_url: str = "",
        jellyfin_api_key: str = "",
        tmdb_api_key: str = "",
        require_outbound: Callable[[str], None] | None = None,
    ) -> None:
        self.jellyfin = JellyfinClient(jellyfin_url, jellyfin_api_key, require_outbound)
        self.tmdb = TMDbClient(tmdb_api_key, require_outbound)

    def summary(self) -> dict[str, Any]:
        return {
            "jellyfin": self.jellyfin.summary(),
            "tmdb": self.tmdb.summary(),
        }

    def discover(self, query: str = "") -> dict[str, Any]:
        return self.tmdb.discover(query)

    def library(self, query: str = "") -> dict[str, Any]:
        return self.jellyfin.library(query)

    def details(self, provider: str, provider_id: str, media_type: str) -> dict[str, Any]:
        provider_name = (provider or "").strip().lower()
        if provider_name == "jellyfin":
            return self.jellyfin.details(provider_id, media_type)
        return self.tmdb.details(provider_id, media_type)

    def stream_jellyfin_image(
        self,
        item_id: str,
        image_type: str,
        max_width: int | None = None,
    ) -> tuple[bytes, str]:
        return self.jellyfin.fetch_image(item_id, image_type, max_width=max_width)

    def jellyfin_url_for(
        self,
        title: str,
        year: str = "",
        media_type: str = "movie",
        season: int | None = None,
        episode: int | None = None,
    ) -> str:
        return self.jellyfin.find_url(title, year=year, media_type=media_type, season=season, episode=episode)

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
    ) -> None:
        super().__init__(ttl_seconds=3600)
        self.api_key = (api_key or "").strip()
        self.require_outbound = require_outbound or (lambda _context: None)

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

        ttl = 86400 if not query else 3600
        return self._cached(f"discover:{query}", load, ttl_seconds=ttl)

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


class JellyfinClient(_CachedClient):
    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        require_outbound: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(ttl_seconds=120)
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.require_outbound = require_outbound or (lambda _context: None)

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def summary(self) -> dict[str, Any]:
        return {
            "configured": self.configured(),
            "url": self.base_url,
        }

    def library(self, query: str = "") -> dict[str, Any]:
        query = (query or "").strip()
        if not self.configured():
            return {
                "configured": False,
                "query": query,
                "counts": {"movies": 0, "series": 0},
                "sections": [],
                "items": [],
                "source": "jellyfin",
            }

        def load() -> dict[str, Any]:
            self.require_outbound("Jellyfin library")
            all_items = self._request_json(
                "/Items",
                {
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Series",
                    "Fields": "Overview,CommunityRating,ProductionYear,DateCreated,PremiereDate",
                    "SortBy": "DateCreated,SortName",
                    "SortOrder": "Descending",
                    "Limit": "120",
                },
            ).get("Items", [])
            mapped = [self._map_item(item) for item in all_items]
            items = [item for item in mapped if item]
            if query:
                needle = query.lower()
                items = [
                    item
                    for item in items
                    if needle in item["title"].lower()
                    or needle in item.get("overview", "").lower()
                ]
            movies = [item for item in items if item["media_type"] == "movie"]
            series = [item for item in items if item["media_type"] == "tv"]
            return {
                "configured": True,
                "query": query,
                "counts": {
                    "movies": len(movies),
                    "series": len(series),
                },
                "source": "jellyfin",
                "sections": [
                    {"id": "recent_movies", "title": "Recently Added Movies", "items": movies[:12]},
                    {"id": "series_library", "title": "Series Library", "items": series[:12]},
                ],
                "items": items[:36],
            }

        return self._cached(f"library:{query}", load)

    def details(self, provider_id: str, media_type: str) -> dict[str, Any]:
        provider_id = (provider_id or "").strip()
        media_type = (media_type or "movie").strip().lower()
        if not self.configured():
            return {"configured": False, "provider": "jellyfin", "media_type": media_type, "seasons": []}
        if not provider_id:
            return {"configured": True, "provider": "jellyfin", "media_type": media_type, "seasons": []}

        def load() -> dict[str, Any]:
            self.require_outbound("Jellyfin details")
            item = self._request_json(
                f"/Items/{urllib.parse.quote(provider_id)}",
                {"Fields": "Overview,CommunityRating,ProductionYear"},
            )
            title = str(item.get("Name") or "").strip()
            year = str(item.get("ProductionYear") or "")
            if media_type != "tv":
                return {
                    "configured": True,
                    "provider": "jellyfin",
                    "provider_id": provider_id,
                    "media_type": media_type,
                    "title": title,
                    "year": year,
                    "seasons": [],
                }

            seasons_payload = self._request_json(f"/Shows/{urllib.parse.quote(provider_id)}/Seasons", {})
            seasons = []
            for season in seasons_payload.get("Items", []):
                season_id = str(season.get("Id") or "").strip()
                season_number = int(season.get("IndexNumber") or 0)
                if not season_id:
                    continue
                episodes_payload = self._request_json(
                    f"/Shows/{urllib.parse.quote(provider_id)}/Episodes",
                    {
                        "SeasonId": season_id,
                        "Fields": "Overview,CommunityRating,ProductionYear,PremiereDate",
                    },
                )
                episodes = []
                for episode in episodes_payload.get("Items", []):
                    episode_number = int(episode.get("IndexNumber") or 0)
                    code = f"S{season_number:02d}E{episode_number:02d}" if season_number and episode_number else ""
                    episodes.append(
                        {
                            "id": str(episode.get("Id") or ""),
                            "title": str(episode.get("Name") or code or "Episode"),
                            "episode_number": episode_number,
                            "season_number": season_number,
                            "air_date": str(episode.get("PremiereDate") or ""),
                            "search_hint": " ".join(part for part in [title, year, code] if part).strip(),
                            "jellyfin_url": f"{self.base_url}/web/#/details?id={urllib.parse.quote(str(episode.get('Id') or ''))}",
                            "owned": True,
                        }
                    )
                seasons.append(
                    {
                        "id": season_id,
                        "title": str(season.get("Name") or f"Season {season_number or len(seasons) + 1}"),
                        "season_number": season_number,
                        "episode_count": len(episodes),
                        "search_hint": " ".join(part for part in [title, year, f"Season {season_number}"] if part).strip(),
                        "jellyfin_url": f"{self.base_url}/web/#/details?id={urllib.parse.quote(season_id)}",
                        "owned": True,
                        "episodes": episodes,
                    }
                )

            return {
                "configured": True,
                "provider": "jellyfin",
                "provider_id": provider_id,
                "media_type": "tv",
                "title": title,
                "year": year,
                "jellyfin_url": f"{self.base_url}/web/#/details?id={urllib.parse.quote(provider_id)}",
                "owned": True,
                "seasons": seasons,
            }

        return self._cached(f"details:jellyfin:{media_type}:{provider_id}", load, ttl_seconds=600)

    def fetch_image(
        self,
        item_id: str,
        image_type: str,
        max_width: int | None = None,
    ) -> tuple[bytes, str]:
        if not self.configured():
            raise RuntimeError("Jellyfin is not configured")
        self.require_outbound("Jellyfin artwork")
        params = {}
        if max_width:
            params["maxWidth"] = str(max_width)
        path = f"/Items/{urllib.parse.quote(item_id)}/Images/{urllib.parse.quote(image_type)}"
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(
            f"{self.base_url}{path}{query}",
            headers={
                "X-Emby-Token": self.api_key,
                "Accept": "image/*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read(), response.headers.get_content_type() or "image/jpeg"
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Jellyfin image request failed with {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("Jellyfin image request failed") from exc

    def find_url(
        self,
        title: str,
        year: str = "",
        media_type: str = "movie",
        season: int | None = None,
        episode: int | None = None,
    ) -> str:
        title = (title or "").strip()
        year = (year or "").strip()
        media_type = (media_type or "movie").strip().lower()
        if not self.configured() or not title:
            return ""
        try:
            return self._cached(
                f"find-url:{media_type}:{title}:{year}:{season or ''}:{episode or ''}",
                lambda: self._find_url_uncached(title, year, media_type, season, episode),
                ttl_seconds=300,
            )
        except RuntimeError:
            return ""

    def _find_url_uncached(
        self,
        title: str,
        year: str,
        media_type: str,
        season: int | None,
        episode: int | None,
    ) -> str:
        self.require_outbound("Jellyfin item lookup")
        include_types = "Series" if media_type == "tv" else "Movie"
        items = self._request_json(
            "/Items",
            {
                "Recursive": "true",
                "IncludeItemTypes": include_types,
                "SearchTerm": title,
                "Fields": "ProductionYear",
                "Limit": "20",
            },
        ).get("Items", [])
        match = self._best_title_match(items, title, year)
        item_id = str(match.get("Id") or "").strip() if match else ""
        if not item_id:
            return ""
        if media_type != "tv":
            return self._details_url(item_id)

        if season:
            seasons_payload = self._request_json(f"/Shows/{urllib.parse.quote(item_id)}/Seasons", {})
            season_id = ""
            for season_item in seasons_payload.get("Items", []):
                if int(season_item.get("IndexNumber") or 0) == season:
                    season_id = str(season_item.get("Id") or "").strip()
                    break
            if episode and season_id:
                episodes_payload = self._request_json(
                    f"/Shows/{urllib.parse.quote(item_id)}/Episodes",
                    {"SeasonId": season_id},
                )
                for episode_item in episodes_payload.get("Items", []):
                    if int(episode_item.get("IndexNumber") or 0) == episode:
                        episode_id = str(episode_item.get("Id") or "").strip()
                        if episode_id:
                            return self._details_url(episode_id)
            if season_id:
                return self._details_url(season_id)
        return self._details_url(item_id)

    def _details_url(self, item_id: str) -> str:
        return f"{self.base_url}/web/#/details?id={urllib.parse.quote(item_id)}"

    def _best_title_match(self, items: list[dict[str, Any]], title: str, year: str) -> dict[str, Any] | None:
        normalized_title = _normalize_media_title(title)
        year = str(year or "").strip()
        if not items:
            return None
        for item in items:
            if _normalize_media_title(str(item.get("Name") or "")) == normalized_title:
                item_year = str(item.get("ProductionYear") or "").strip()
                if not year or item_year == year:
                    return item
        for item in items:
            if _normalize_media_title(str(item.get("Name") or "")) == normalized_title:
                return item
        return items[0]

    def _request_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers={
                "Accept": "application/json",
                "X-Emby-Token": self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            LOGGER.warning("jellyfin request failed status=%s path=%s detail=%s", exc.code, path, detail[:240])
            raise RuntimeError(f"Jellyfin request failed with {exc.code}") from exc
        except urllib.error.URLError as exc:
            LOGGER.warning("jellyfin request failed path=%s error=%s", path, exc)
            raise RuntimeError("Jellyfin request failed") from exc

    def _map_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(item.get("Id") or "").strip()
        title = str(item.get("Name") or "").strip()
        item_type = str(item.get("Type") or "").strip().lower()
        if not item_id or not title or item_type not in {"movie", "series"}:
            return None
        year_value = item.get("ProductionYear")
        year = str(year_value) if year_value else ""
        media_type = "tv" if item_type == "series" else "movie"
        poster_url = f"/api/media/jellyfin/image/{item_id}/Primary?max_width=360"
        backdrop_url = f"/api/media/jellyfin/image/{item_id}/Backdrop?max_width=960"
        return {
            "id": f"jellyfin-{item_id}",
            "provider": "jellyfin",
            "provider_id": item_id,
            "title": title,
            "year": year,
            "media_type": media_type,
            "overview": str(item.get("Overview") or ""),
            "rating": float(item.get("CommunityRating") or 0.0),
            "poster_url": poster_url,
            "backdrop_url": backdrop_url,
            "jellyfin_url": f"{self.base_url}/web/#/details?id={urllib.parse.quote(item_id)}",
            "search_hint": " ".join(part for part in [title, year] if part).strip(),
            "actions": {
                "auto_find": True,
                "open_jellyfin": True,
            },
        }


def _normalize_media_title(value: str) -> str:
    return " ".join((value or "").strip().lower().split())
