from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    megadebrid_login: str = Field("", alias="MEGADEBRID_LOGIN")
    megadebrid_password: str = Field("", alias="MEGADEBRID_PASSWORD")

    media_path_movies: str = Field("/media/Movies", alias="MEDIA_PATH_MOVIES")
    media_path_series: str = Field("/media/Series", alias="MEDIA_PATH_SERIES")
    media_path_anime_movies: str = Field("/media/Anime-Movies", alias="MEDIA_PATH_ANIME_MOVIES")
    media_path_anime_series: str = Field("/media/Anime-Series", alias="MEDIA_PATH_ANIME_SERIES")
    media_path_uncategorized: str = Field("/media/Uncategorized", alias="MEDIA_PATH_UNCATEGORIZED")

    temp_path: str = Field("/temp", alias="TEMP_PATH")

    max_concurrent_downloads: int = Field(2, alias="MAX_CONCURRENT_DOWNLOADS")

    database_path: str = Field("/app/data/plex-get.db", alias="DATABASE_PATH")

    web_username: str = Field("", alias="WEB_USERNAME")
    web_password: str = Field("", alias="WEB_PASSWORD")

    web_port: int = Field(8000, alias="WEB_PORT")

    dcrypt_base_url: str = Field("https://dcrypt.it", alias="DCRYPT_BASE_URL")

    def media_path_for(self, media_type: str) -> Path:
        mapping = {
            "movie": self.media_path_movies,
            "series": self.media_path_series,
            "anime_movie": self.media_path_anime_movies,
            "anime_series": self.media_path_anime_series,
            "uncategorized": self.media_path_uncategorized,
        }
        if media_type not in mapping:
            raise ValueError(f"Unknown media type: {media_type}")
        return Path(mapping[media_type])


@lru_cache
def get_settings() -> Settings:
    return Settings()
