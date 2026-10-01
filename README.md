# RD Media Downloader v2

Uses the Stremio addon protocol to query a configured Torrentio endpoint for
individual episode streams. Candidates are ranked by the seeder count parsed
from the returned stream metadata.

The UI does **not** automatically submit the highest-ranked Torrentio result.
You select a candidate, then the existing Real-Debrid downloader handles the
magnet.

## Layout

- `/media/tv`
- `/media/movies`

## Configure

```bash
cp .env.example .env
nano .env
```

Set:

```env
REAL_DEBRID_TOKEN=...
TORRENTIO_URL=https://torrentio.strem.fun/<your-config>
```

Torrentio's Stremio stream protocol uses `/stream/series/<imdb>:<season>:<episode>.json`;
Stremio documents the series video ID format as `imdb:season:episode`.

## Run

```bash
docker compose up -d --build
```

Open:

```text
http://SERVER-IP:8080
```

The `/api/discover` endpoint returns ranked candidates for one episode.
The `/api/download` endpoint accepts a magnet explicitly selected by the user
and sends it through Real-Debrid.
