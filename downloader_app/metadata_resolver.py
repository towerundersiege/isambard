from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


INVALID_PATH_CHARS = re.compile(r'[<>:"/\\\\|?*\x00-\x1f]')
SPACE_RE = re.compile(r"\s+")
TITLE_YEAR_RE = re.compile(r"^(?P<title>.+?)(?:\s*\((?P<year>\d{4})\))?$")
SEASON_EPISODE_SUFFIX_RE = re.compile(
    r"(?:\s*[-:|]?\s*(?:season\s*\d+|episode\s*\d+|s\d{1,2}e\d{1,2}|ep\.?\s*\d+).*)$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ResolvedMedia:
    display_title: str
    output_template: Path
    media_type: str
    series_name: str = ""
    series_year: str = ""
    season: int | None = None
    episode: int | None = None
    youtube_id: str = ""
    channel_id: str = ""
    upload_date: str = ""


def sanitize_path_segment(value: str) -> str:
    cleaned = INVALID_PATH_CHARS.sub("", value or "")
    cleaned = SPACE_RE.sub(" ", cleaned).strip().strip(".")
    return cleaned or "Untitled"


def normalize_series_title(value: str) -> str:
    base = SPACE_RE.sub(" ", (value or "").strip())
    base = re.sub(r"\s*[-|]\s*Y?Flix.*$", "", base, flags=re.IGNORECASE).strip()
    base = re.sub(r"\s*-\s*Watch Now on Dashflix\s*$", "", base, flags=re.IGNORECASE).strip()
    base = re.sub(r"\s*[-|]\s*DashFlix.*$", "", base, flags=re.IGNORECASE).strip()
    base = re.sub(r"\s*[-|]\s*DopeBox.*$", "", base, flags=re.IGNORECASE).strip()
    return base or "Untitled"


def parse_title_and_year(value: str) -> tuple[str, str]:
    normalized = normalize_series_title(value)
    match = TITLE_YEAR_RE.match(normalized)
    if not match:
        return normalized, ""
    return normalize_series_title(match.group("title")), match.group("year") or ""


def strip_episode_context(value: str) -> str:
    stripped = SEASON_EPISODE_SUFFIX_RE.sub("", normalize_series_title(value))
    stripped = re.sub(r"\s*[-:|]\s*$", "", stripped).strip()
    return stripped or normalize_series_title(value)


def is_generic_episode_title(value: str, episode: int | None) -> bool:
    text = SPACE_RE.sub(" ", (value or "").strip()).lower()
    if not text:
        return True
    if text in {"episode", "ep", "pilot", "finale"}:
        return False
    if episode is None:
        return False
    candidates = {
        str(episode),
        f"episode {episode}",
        f"ep {episode}",
        f"ep. {episode}",
        f"e{episode}",
    }
    return text in candidates


class MetadataResolver:
    def resolve(self, requested_title: str, metadata: dict[str, Any] | None = None) -> ResolvedMedia:
        metadata = metadata or {}
        season = self._coerce_int(metadata.get("season"))
        episode = self._coerce_int(metadata.get("episode"))
        page_title = str(metadata.get("page_title") or "")
        raw_title = str(metadata.get("raw_title") or requested_title or page_title or "Untitled")
        series_name, series_year = self._extract_series(raw_title, page_title, metadata)

        if season is not None and episode is not None:
            display_series = self._display_series(series_name, series_year)
            filename = f"{display_series} - S{season:02d}E{episode:02d}"
            season_folder = f"{display_series} - S{season:02d}"
            output_template = (
                Path("tv")
                / sanitize_path_segment(display_series)
                / sanitize_path_segment(season_folder)
                / f"{sanitize_path_segment(filename)}.%(ext)s"
            )
            return ResolvedMedia(
                display_title=filename,
                output_template=output_template,
                media_type="tv",
                series_name=display_series,
                series_year=series_year,
                season=season,
                episode=episode,
            )

        movie_title, movie_year = parse_title_and_year(raw_title)
        if not movie_year:
            movie_year = str(metadata.get("year") or "")
        display_movie = self._display_series(movie_title, movie_year)
        output_template = (
            Path("movies")
            / sanitize_path_segment(display_movie)
            / f"{sanitize_path_segment(display_movie)}.%(ext)s"
        )
        return ResolvedMedia(
            display_title=display_movie,
            output_template=output_template,
            media_type="movie",
            series_name=display_movie,
            series_year=movie_year,
        )

    def _extract_series(self, raw_title: str, page_title: str, metadata: dict[str, Any]) -> tuple[str, str]:
        year = str(metadata.get("year") or "")
        candidates = [
            str(metadata.get("series_name") or ""),
            raw_title,
            page_title,
        ]
        for candidate in candidates:
            title, parsed_year = parse_title_and_year(strip_episode_context(candidate))
            if title and title.lower() != "untitled":
                if not year and parsed_year:
                    year = parsed_year
                return title, year
        return "Untitled", year

    def _display_series(self, title: str, year: str) -> str:
        normalized = strip_episode_context(title)
        if year:
            return f"{normalized} ({year})"
        return normalized

    def _coerce_int(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return None

    def resolve_youtube(self, metadata: dict[str, Any]) -> ResolvedMedia:
        raw_title = str(metadata.get("title") or metadata.get("raw_title") or "Untitled")
        channel_id = sanitize_path_segment(
            str(
                metadata.get("channel_id")
                or metadata.get("uploader_id")
                or metadata.get("uploader")
                or "unknown-channel"
            )
        )
        upload_date = re.sub(r"[^0-9]", "", str(metadata.get("upload_date") or ""))[:8]
        if len(upload_date) != 8:
            upload_date = "unknown-date"
        youtube_id = str(metadata.get("youtube_id") or metadata.get("id") or "")
        filename = f"{upload_date} - {sanitize_path_segment(raw_title)}"
        output_template = (
            Path("youtube")
            / channel_id
            / f"{filename}.%(ext)s"
        )
        return ResolvedMedia(
            display_title=raw_title.strip() or "Untitled",
            output_template=output_template,
            media_type="youtube",
            youtube_id=youtube_id,
            channel_id=channel_id,
            upload_date=upload_date,
        )
