# Plex-Get

Automated downloader for movies and series for Plex, running on Unraid via Docker.

## Features

- Debrids links via the [Mega-Debrid](https://www.mega-debrid.eu) API.
- Accepts single links, lists, or `.dlc` containers.
- Downloads into a fast cache/temp path on SSD, then extracts and moves the main video file to the correct final media path (HDD/array).
- Smart folder layout:
  - `Movie` / `Anime-Movie` → `Movies/<Title> (<Year>)/<file>.mkv`
  - `Series` / `Anime-Series` → `Series/<Show>/Season 0X/<file>.mkv` (Specials → `Specials`)
  - `Uncategorized` → flat folder under `Uncategorized/`
- Configurable archive password list (tried in order, configurable at runtime).
- Configurable concurrent downloads (default 2).
- Web UI (port 8000) and CLI.
- Live progress via WebSocket.

## Setup

1. Copy `.env.example` to `.env` and fill in your Mega-Debrid login/password and the host paths for your media folders (mounted into the container).
2. `docker compose up -d --build`
3. Open `http://<unraid-host>:8000`

## CLI

```
python -m plex_get new --type series "https://link1" "https://link2"
python -m plex_get watch <task_id>
python -m plex_get password add "mysecret"
python -m plex_get list --last-24h
```

## Project layout

- `plex_get/api.py` – FastAPI web app + WebSocket
- `plex_get/manager.py` – background download/extract/move worker
- `plex_get/debrid.py` – Mega-Debrid client
- `plex_get/dlc.py` – .dlc container parser
- `plex_get/naming.py` – filename parser & destination path builder
- `plex_get/extractor.py` – rar extraction + safe move
- `plex_get/cli.py` – Click-based CLI
- `plex_get/web/` – static UI (HTML/CSS/JS)
