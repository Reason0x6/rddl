# RD Media Downloader v2

Uses the Stremio addon protocol to query a configured Torrentio endpoint. TV
searches look up a season's episode list and show the two top reported-seeder
candidates for each episode. Movie searches show ranked candidates. Select
multiple releases and send them together to Real-Debrid or TorBox.

The app is protected by a login. Signed-in users can search IMDb titles and add
movies or TV shows to the shared request queue, then check or update request
status. The downloader never submits releases automatically: choose the
release(s), then the configured debrid service processes your selection as an
asynchronous batch.

## Layout

- `/media/TV/<show>/S<season>`
- `/media/Movies`

## Configure

```bash
cp .env.example .env
nano .env
```

Set:

```env
REAL_DEBRID_TOKEN=...
TORBOX_API_KEY=...
TORRENTIO_URL=https://torrentio.strem.fun/<your-config>
APP_USERNAME=admin
APP_PASSWORD=choose-a-strong-password
```

Either provider can be configured independently. The app enables each provider
whose credential is present. If both credentials are set, the downloader asks
which provider should handle each batch. Set `TORBOX_API_KEY` in the service
environment to enable TorBox.

TorBox downloads the torrent into its cloud account before the app retrieves
the selected video files into `/media`; Real-Debrid continues to select the
requested files in its torrent before retrieval.

On first run, if `APP_PASSWORD` is blank, the app generates a random password
and prints it once in the container logs. Sign in with the configured username
and that password. The generated credential verifier, session key, and request
queue are persisted in `./data`. Set `AUTH_COOKIE_SECURE=1` when the app is
served only over HTTPS.

Admins can create requestor accounts, reset their passwords, and remove access
from the **Accounts** page. Credentials are stored as salted password hashes in
`./data/users.json`. `APP_REQUESTOR_PASSWORD` bootstraps the legacy `requestor`
account when it is first created; after that, its password is managed in the
app and is not overwritten on restart.

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
The Catalog page lists movies and TV shows found on disk, including seasons and
episode counts.
The Requests page searches Cinemeta/IMDb titles and stores requests in SQLite
under `/data/requests.sqlite3`.
The `/api/download-batches` endpoint starts selected downloads asynchronously;
the Downloads page lists saved batches and refreshes active status automatically.
Batch state is stored in `/data/downloads.sqlite3`; queued work resumes after an
app restart. Poll `/api/download-batches/{job_id}` for an individual batch.
Up to three downloads run concurrently. TV files are stored in a show folder
and an `S01`-style season folder. Movie files are stored directly in
`/media/Movies`.

Download progress is also written to the container logs:

```bash
docker compose logs -f rd-downloader
```
