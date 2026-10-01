# RD Media Downloader v2

Uses the Stremio addon protocol to query a configured Torrentio endpoint. TV
searches look up a season's episode list and show the top reported-seeder
candidate for each episode. Movie searches show ranked candidates.

The UI does **not** automatically submit the highest-ranked Torrentio result.
You select a candidate, then the existing Real-Debrid downloader handles the
magnet.

## Layout

- `/media/tv/<show>/S<season>`
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

The `/api/discover-season` endpoint returns one top candidate per episode.
The `/api/discover-movie` endpoint returns ranked movie candidates.
The `/api/download` endpoint accepts a magnet explicitly selected by the user
and sends it through Real-Debrid. TV files are stored in a show folder and an
`S01`-style season folder. Movie files are stored directly in `/media/movies`.
