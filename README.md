# RD Media Downloader v2

Uses the Stremio addon protocol to query a configured Torrentio endpoint. TV
searches look up a season's episode list and show the two top reported-seeder
candidates for each episode. Movie searches show ranked candidates. Select
multiple releases and send them together to Real-Debrid.

The UI never submits releases automatically. You choose the release(s), then
the Real-Debrid downloader processes your selection as an asynchronous batch.

## Layout

- `/media/TV/<show>/S<season>`
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

The `/api/discover-season` endpoint returns the two top candidates per episode.
The `/api/discover-movie` endpoint returns ranked movie candidates.
The `/api/download-batches` endpoint starts selected downloads asynchronously;
poll `/api/download-batches/{job_id}` for progress. Up to three downloads run
concurrently. TV files are stored in a show folder and an `S01`-style season
folder. Movie files are stored directly in `/media/movies`.

Download progress is also written to the container logs:

```bash
docker compose logs -f rd-downloader
```
