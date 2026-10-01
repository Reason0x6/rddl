import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

RD_BASE = "https://api.real-debrid.com/rest/1.0"
TOKEN = os.environ.get("REAL_DEBRID_TOKEN")
ROOT = Path(os.environ.get("DOWNLOAD_ROOT", "/media"))
POLL = int(os.environ.get("POLL_SECONDS", "10"))
TORRENTIO_URL = os.environ.get("TORRENTIO_URL", "").rstrip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
REQUEST_DB = DATA_DIR / "requests.sqlite3"
AUTH_STATE_FILE = DATA_DIR / "auth.json"
SESSION_KEY_FILE = DATA_DIR / "session.key"
RD_REQUEST_ATTEMPTS = 3
RETRYABLE_HTTP_STATUSES = {408, 425, 429}
BLOCKED_RELEASE_PATTERNS = [
    re.compile(r"web-dl|webrip|bdrip|hdrip|dvdrip", re.I),
    re.compile(r"(?:bluray\.x264|hdtv\.x264|hdtv\.xvid|web\.x264|web\.h264)", re.I),
]
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg",
    ".m2ts", ".ts", ".webm", ".vob", ".ogv", ".3gp", ".flv", ".divx",
}

app = FastAPI(title="RD Media Downloader", version="2.0.0")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("rd_downloader")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DATA_DIR.mkdir(parents=True, exist_ok=True)


def password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


def initialize_auth():
    configured_password = os.environ.get("APP_PASSWORD")
    configured_username = os.environ.get("APP_USERNAME")
    if configured_password:
        username = configured_username or "admin"
        salt = secrets.token_bytes(16)
        return username, salt, password_hash(configured_password, salt)

    if AUTH_STATE_FILE.exists():
        saved = json.loads(AUTH_STATE_FILE.read_text())
        return saved["username"], bytes.fromhex(saved["salt"]), bytes.fromhex(saved["password_hash"])

    username = configured_username or "admin"
    initial_password = secrets.token_urlsafe(18)
    salt = secrets.token_bytes(16)
    verifier = password_hash(initial_password, salt)
    AUTH_STATE_FILE.write_text(json.dumps({
        "username": username,
        "salt": salt.hex(),
        "password_hash": verifier.hex(),
    }))
    logger.warning("Initial app login created username=%s password=%s; set APP_PASSWORD to replace it",
                   username, initial_password)
    return username, salt, verifier


def load_session_key() -> bytes:
    configured = os.environ.get("SESSION_SECRET")
    if configured:
        return configured.encode()
    if SESSION_KEY_FILE.exists():
        return SESSION_KEY_FILE.read_bytes()
    key = secrets.token_bytes(32)
    SESSION_KEY_FILE.write_bytes(key)
    SESSION_KEY_FILE.chmod(0o600)
    return key


AUTH_USERNAME, AUTH_SALT, AUTH_PASSWORD_HASH = initialize_auth()
SESSION_KEY = load_session_key()


def init_request_db():
    with sqlite3.connect(REQUEST_DB) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
            imdb_id TEXT NOT NULL,
            title TEXT NOT NULL,
            year TEXT,
            season INTEGER,
            requested_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'requested',
            created_at TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS request_status_idx ON requests(status, created_at)")


init_request_db()

SESSION_COOKIE = "rd_session"
SESSION_TTL = 12 * 60 * 60
AUTH_COOKIE_SECURE = os.environ.get("AUTH_COOKIE_SECURE", "0") == "1"
LOGIN_FAILURES = {}


def make_session(username: str) -> str:
    payload = base64.urlsafe_b64encode(
        f"{username}|{int(time.time()) + SESSION_TTL}".encode()
    ).decode().rstrip("=")
    signature = hmac.new(SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def session_username(cookie: str | None) -> str | None:
    if not cookie or "." not in cookie:
        return None
    payload, signature = cookie.rsplit(".", 1)
    expected = hmac.new(SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        username, expires = decoded.rsplit("|", 1)
        if int(expires) < int(time.time()) or username != AUTH_USERNAME:
            return None
        return username
    except (ValueError, UnicodeDecodeError):
        return None


@app.middleware("http")
async def require_login(request: Request, call_next):
    username = session_username(request.cookies.get(SESSION_COOKIE))
    request.state.username = username
    if request.url.path.startswith("/api/") and request.url.path not in {
        "/api/login", "/api/session",
    } and not username:
        return JSONResponse({"detail": "Login required"}, status_code=401)
    if username and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if origin and host and urlsplit(origin).netloc != host:
            return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
    return await call_next(request)


class DownloadRequest(BaseModel):
    magnet: str = Field(min_length=10)
    media_type: Literal["tv", "movies"]
    title: str | None = None
    season: int | None = Field(default=None, ge=1)
    label: str | None = None
    selection_key: str | None = None
    release_name: str = Field(min_length=1, max_length=500)


class DownloadBatchRequest(BaseModel):
    downloads: list[DownloadRequest] = Field(min_length=1, max_length=30)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class MediaRequestInput(BaseModel):
    media_type: Literal["movie", "series"]
    imdb_id: str = Field(pattern=r"^tt\d+$", max_length=20)
    title: str = Field(min_length=1, max_length=200)
    year: str | None = Field(default=None, max_length=20)
    season: int | None = Field(default=None, ge=1, le=100)


class RequestStatusInput(BaseModel):
    status: Literal["requested", "downloading", "completed", "declined"]


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    return value[:180] or "Unknown"


def rd_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def parse_seeders(stream: dict) -> int:
    """Best-effort extraction from Torrentio's human-readable fields.

    Torrentio/Stremio stream objects don't guarantee a dedicated seeder field.
    Depending on configuration, seeder information may appear in title/name/
    description. We deliberately expose the parsed value as a ranking hint,
    not as a guarantee.
    """
    text = " ".join(str(stream.get(k, "")) for k in
                    ("name", "title", "description", "behaviorHints"))
    patterns = [
        r"[👤🌱]\s*(\d[\d,]*)",
        r"(?:seed(?:ers)?|seeds|peers?)\s*[:=]?\s*(\d[\d,]*)",
        r"\b(\d[\d,]*)\s*(?:seeders|seeds|peers)\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return -1


def release_is_allowed(*names: str | None) -> bool:
    torrent_name = " ".join(name for name in names if name)
    return not any(pattern.search(torrent_name) for pattern in BLOCKED_RELEASE_PATTERNS)


def stream_to_magnet(stream: dict) -> str | None:
    ih = stream.get("infoHash") or stream.get("infohash")
    if not ih:
        # Some addons may expose a magnet directly in url.
        url = stream.get("url")
        if isinstance(url, str) and url.startswith("magnet:?"):
            return url
        return None

    # Stremio stream objects use infoHash for torrent identity.
    return f"magnet:?xt=urn:btih:{ih}"


def rank_stream(stream: dict) -> tuple:
    seeders = parse_seeders(stream)
    text = " ".join(str(stream.get(k, "")) for k in
                    ("name", "title", "description")).lower()

    # Secondary ordering only; seeders remain the primary criterion.
    resolution = 0
    if "2160p" in text or "4k" in text:
        resolution = 2160
    elif "1080p" in text:
        resolution = 1080
    elif "720p" in text:
        resolution = 720
    elif "480p" in text:
        resolution = 480

    return (seeders, resolution)


async def torrentio_streams(imdb_id: str, season: int, episode: int):
    if not TORRENTIO_URL:
        raise HTTPException(500, "TORRENTIO_URL is not configured")

    video_id = f"{imdb_id}:{season}:{episode}"
    url = f"{TORRENTIO_URL}/stream/series/{video_id}.json"

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            url,
            headers={
                "User-Agent": "Stremio/4.4.168",
                "Accept": "application/json",
            },
        )
        if r.status_code >= 400:
            raise HTTPException(r.status_code,
                                f"Torrentio returned HTTP {r.status_code}")
        data = r.json()

    results = []
    for stream in data.get("streams", []):
        if not release_is_allowed(stream.get("name"), stream.get("title")):
            continue
        magnet = stream_to_magnet(stream)
        item = {
            "name": stream.get("name"),
            "title": stream.get("title"),
            "description": stream.get("description"),
            "infoHash": stream.get("infoHash"),
            "seeders": parse_seeders(stream),
            "resolution": (
                "2160p" if "2160p" in str(stream).lower() or "4k" in str(stream).lower()
                else "1080p" if "1080p" in str(stream).lower()
                else "720p" if "720p" in str(stream).lower()
                else None
            ),
            "magnet": magnet,
        }
        if magnet:
            results.append(item)

    resolution_order = {"2160p": 2160, "1080p": 1080, "720p": 720, "480p": 480}
    results.sort(
        key=lambda x: (x["seeders"], resolution_order.get(x["resolution"], 0)),
        reverse=True,
    )
    return results


async def season_metadata(imdb_id: str):
    url = f"https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, "Stremio metadata service returned an error") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise HTTPException(502, "Could not read series metadata from Stremio") from exc

    meta = payload.get("meta") or {}
    videos = meta.get("videos")
    if not isinstance(videos, list):
        raise HTTPException(404, "No episode list found for this IMDb ID")
    return meta


async def torrentio_movie_streams(imdb_id: str):
    if not TORRENTIO_URL:
        raise HTTPException(500, "TORRENTIO_URL is not configured")

    url = f"{TORRENTIO_URL}/stream/movie/{imdb_id}.json"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={
                "User-Agent": "Stremio/4.4.168",
                "Accept": "application/json",
            },
        )
        if response.status_code >= 400:
            raise HTTPException(response.status_code,
                                f"Torrentio returned HTTP {response.status_code}")
        data = response.json()

    results = []
    for stream in data.get("streams", []):
        if not release_is_allowed(stream.get("name"), stream.get("title")):
            continue
        magnet = stream_to_magnet(stream)
        if not magnet:
            continue
        text = " ".join(str(stream.get(key, "")) for key in ("name", "title", "description")).lower()
        resolution = next((value for value in ("2160p", "1080p", "720p", "480p")
                           if value in text), None)
        results.append({
            "name": stream.get("name"),
            "title": stream.get("title"),
            "description": stream.get("description"),
            "infoHash": stream.get("infoHash"),
            "seeders": parse_seeders(stream),
            "resolution": resolution,
            "magnet": magnet,
        })

    resolution_order = {"2160p": 2160, "1080p": 1080, "720p": 720, "480p": 480}
    results.sort(
        key=lambda item: (item["seeders"], resolution_order.get(item["resolution"], 0)),
        reverse=True,
    )
    return results


@app.get("/")
async def index(request: Request):
    if not request.state.username:
        return FileResponse("/app/static/login.html", headers={"Cache-Control": "no-store"})
    return FileResponse("/app/static/index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/session")
async def get_session(request: Request):
    return {"authenticated": bool(request.state.username), "username": request.state.username}


@app.post("/api/login")
async def login(payload: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    failures, retry_after = LOGIN_FAILURES.get(client_ip, (0, 0))
    if retry_after > time.time():
        raise HTTPException(429, "Too many login attempts. Try again shortly.")
    candidate_hash = password_hash(payload.password, AUTH_SALT)
    valid_username = hmac.compare_digest(payload.username, AUTH_USERNAME)
    valid_password = hmac.compare_digest(candidate_hash, AUTH_PASSWORD_HASH)
    if not (valid_username and valid_password):
        failures += 1
        LOGIN_FAILURES[client_ip] = (0, time.time() + 60) if failures >= 5 else (failures, 0)
        raise HTTPException(401, "Incorrect username or password")
    LOGIN_FAILURES.pop(client_ip, None)
    response = JSONResponse({"authenticated": True, "username": AUTH_USERNAME})
    response.set_cookie(
        SESSION_COOKIE,
        make_session(AUTH_USERNAME),
        max_age=SESSION_TTL,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/logout")
async def logout():
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/title-search")
async def search_titles(
    media_type: Literal["movie", "series"] = Query(...),
    query: str = Query(..., min_length=2, max_length=100),
):
    clean_query = query.strip()
    if re.fullmatch(r"tt\d+", clean_query, re.I):
        url = f"https://v3-cinemeta.strem.io/meta/{media_type}/{clean_query}.json"
        result_key = "meta"
    else:
        url = (f"https://v3-cinemeta.strem.io/catalog/{media_type}/top/"
               f"search={quote(clean_query, safe='')}.json")
        result_key = "metas"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "IMDb title search is temporarily unavailable") from exc

    raw_results = payload.get(result_key)
    if isinstance(raw_results, dict):
        raw_results = [raw_results]
    if not isinstance(raw_results, list):
        raw_results = []
    results = []
    for meta in raw_results:
        if not isinstance(meta, dict):
            continue
        imdb_id = meta.get("imdb_id") or meta.get("id")
        title = meta.get("name")
        if not imdb_id or not title:
            continue
        results.append({
            "imdb_id": imdb_id,
            "title": title,
            "year": str(meta.get("releaseInfo") or meta.get("year") or ""),
            "poster": meta.get("poster"),
            "media_type": media_type,
        })
    return {"results": results[:20]}


def request_row(row):
    return {
        "id": row[0], "media_type": row[1], "imdb_id": row[2], "title": row[3],
        "year": row[4], "season": row[5], "requested_by": row[6],
        "status": row[7], "created_at": row[8],
    }


@app.get("/api/requests")
async def list_requests():
    with sqlite3.connect(REQUEST_DB) as db:
        rows = db.execute(
            "SELECT id, media_type, imdb_id, title, year, season, requested_by, status, created_at "
            "FROM requests ORDER BY id DESC LIMIT 300"
        ).fetchall()
    return {"requests": [request_row(row) for row in rows]}


@app.post("/api/requests", status_code=201)
async def create_media_request(payload: MediaRequestInput, request: Request):
    season = payload.season if payload.media_type == "series" else None
    with sqlite3.connect(REQUEST_DB) as db:
        existing = db.execute(
            "SELECT id FROM requests WHERE imdb_id=? AND season IS ? "
            "AND status IN ('requested', 'downloading') LIMIT 1",
            (payload.imdb_id, season),
        ).fetchone()
        if existing:
            raise HTTPException(409, "That title is already in the request queue")
        cursor = db.execute(
            "INSERT INTO requests (media_type, imdb_id, title, year, season, requested_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payload.media_type, payload.imdb_id, payload.title, payload.year, season,
             request.state.username, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        request_id = cursor.lastrowid
        row = db.execute(
            "SELECT id, media_type, imdb_id, title, year, season, requested_by, status, created_at "
            "FROM requests WHERE id=?", (request_id,),
        ).fetchone()
    return {"request": request_row(row)}


@app.patch("/api/requests/{request_id}")
async def update_media_request(request_id: int, payload: RequestStatusInput):
    with sqlite3.connect(REQUEST_DB) as db:
        cursor = db.execute("UPDATE requests SET status=? WHERE id=?",
                            (payload.status, request_id))
        if not cursor.rowcount:
            raise HTTPException(404, "Request not found")
        row = db.execute(
            "SELECT id, media_type, imdb_id, title, year, season, requested_by, status, created_at "
            "FROM requests WHERE id=?", (request_id,),
        ).fetchone()
    return {"request": request_row(row)}


@app.get("/health")
async def health():
    return {
        "ok": True,
        "download_root": str(ROOT),
        "torrentio_configured": bool(TORRENTIO_URL),
    }


@app.get("/api/discover")
async def discover(
    imdb_id: str = Query(..., pattern=r"^tt\d+$"),
    season: int = Query(..., ge=1),
    episode: int = Query(..., ge=1),
):
    """Discover and rank Torrentio streams for one episode.

    This endpoint only reports candidates. It does not automatically submit
    a Torrentio result to Real-Debrid.
    """
    return {
        "imdb_id": imdb_id,
        "season": season,
        "episode": episode,
        "streams": await torrentio_streams(imdb_id, season, episode),
    }


@app.get("/api/discover-season")
async def discover_season(
    imdb_id: str = Query(..., pattern=r"^tt\d+$"),
    season: int = Query(..., ge=1),
):
    """Return the highest-seeder Torrentio candidate for each episode."""
    meta = await season_metadata(imdb_id)
    videos = []
    for video in meta.get("videos", []):
        video_season = video.get("season")
        episode = video.get("episode", video.get("number"))
        if video_season == season and isinstance(episode, int) and episode > 0:
            videos.append((episode, video))

    videos.sort(key=lambda item: item[0])
    if not videos:
        raise HTTPException(404, f"No episodes found for season {season}")

    semaphore = asyncio.Semaphore(5)

    async def episode_result(episode, video):
        async with semaphore:
            try:
                candidates = await torrentio_streams(imdb_id, season, episode)
                return {
                    "episode": episode,
                    "title": video.get("name") or f"Episode {episode}",
                    "choices": candidates[:2],
                    "candidate_count": len(candidates),
                }
            except HTTPException as exc:
                return {
                    "episode": episode,
                    "title": video.get("name") or f"Episode {episode}",
                    "choices": [],
                    "candidate_count": 0,
                    "error": exc.detail,
                }

    episodes = await asyncio.gather(
        *(episode_result(episode, video) for episode, video in videos)
    )
    return {
        "imdb_id": imdb_id,
        "series_title": meta.get("name") or imdb_id,
        "season": season,
        "episodes": episodes,
    }


@app.get("/api/discover-movie")
async def discover_movie(imdb_id: str = Query(..., pattern=r"^tt\d+$")):
    """Return movie candidates ranked by reported seeders."""
    meta_url = f"https://v3-cinemeta.strem.io/meta/movie/{imdb_id}.json"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            response = await client.get(meta_url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, "Stremio metadata service returned an error") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise HTTPException(502, "Could not read movie metadata from Stremio") from exc

    meta = payload.get("meta") or {}
    if not meta:
        raise HTTPException(404, "Movie not found for this IMDb ID")
    return {
        "imdb_id": imdb_id,
        "movie_title": meta.get("name") or imdb_id,
        "streams": await torrentio_movie_streams(imdb_id),
    }


# ---- Existing user-selected RD download workflow ----

async def rd_request(client, method, path, data=None, params=None):
    for attempt in range(1, RD_REQUEST_ATTEMPTS + 1):
        try:
            if method == "POST":
                response = await client.post(
                    RD_BASE + path, headers=rd_headers(), data=data
                )
            else:
                response = await client.get(
                    RD_BASE + path, headers=rd_headers(), params=params
                )
        except httpx.RequestError as exc:
            if attempt == RD_REQUEST_ATTEMPTS:
                raise HTTPException(
                    502, f"Real-Debrid request failed after {attempt} attempts"
                ) from exc
            logger.warning("RD request network error method=%s path=%s attempt=%s/%s",
                           method, path, attempt, RD_REQUEST_ATTEMPTS)
            await asyncio.sleep(attempt)
            continue

        status = response.status_code
        retryable = status in RETRYABLE_HTTP_STATUSES or status >= 500
        if status >= 400 and retryable and attempt < RD_REQUEST_ATTEMPTS:
            logger.warning("RD request returned HTTP %s method=%s path=%s attempt=%s/%s",
                           status, method, path, attempt, RD_REQUEST_ATTEMPTS)
            await asyncio.sleep(attempt)
            continue
        if status >= 400:
            raise HTTPException(status, f"Real-Debrid error: {response.text[:1000]}")
        return response


async def rd_post(client, path, data=None):
    return await rd_request(client, "POST", path, data)


async def rd_get(client, path, params=None):
    return await rd_request(client, "GET", path, params=params)


async def download_direct_link(client, url, target):
    for attempt in range(1, RD_REQUEST_ATTEMPTS + 1):
        try:
            async with client.stream("GET", url) as response:
                status = response.status_code
                retryable = status in RETRYABLE_HTTP_STATUSES or status >= 500
                if status >= 400:
                    if retryable and attempt < RD_REQUEST_ATTEMPTS:
                        logger.warning("direct download returned HTTP %s attempt=%s/%s",
                                       status, attempt, RD_REQUEST_ATTEMPTS)
                        await asyncio.sleep(attempt)
                        continue
                    raise HTTPException(status, f"File download returned HTTP {status}")
                with target.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        output.write(chunk)
            return
        except httpx.RequestError as exc:
            if attempt == RD_REQUEST_ATTEMPTS:
                raise HTTPException(
                    502, f"File download failed after {attempt} attempts"
                ) from exc
            logger.warning("direct download network error attempt=%s/%s",
                           attempt, RD_REQUEST_ATTEMPTS)
            await asyncio.sleep(attempt)


def magnet_info_hash(magnet: str) -> str | None:
    match = re.search(r"(?:[?&])xt=urn:btih:([^&]+)", magnet, re.I)
    if not match:
        return None
    value = match.group(1).strip().lower()
    if re.fullmatch(r"[a-f0-9]{40}", value):
        return value
    if re.fullmatch(r"[a-z2-7]{32}", value, re.I):
        try:
            return base64.b32decode(value.upper()).hex()
        except (binascii.Error, ValueError):
            return None
    return None


async def find_existing_torrent(client, info_hash):
    if not info_hash:
        return None
    torrents = (await rd_get(client, "/torrents", params={"limit": 5000})).json()
    matches = [torrent for torrent in torrents
               if str(torrent.get("hash", "")).lower() == info_hash]
    if not matches:
        return None
    # Reuse a completed copy where possible; otherwise attach to the most
    # progressed copy instead of adding the same magnet again.
    matches.sort(
        key=lambda torrent: (
            torrent.get("status") == "downloaded",
            float(torrent.get("progress") or 0),
            str(torrent.get("added") or ""),
        ),
        reverse=True,
    )
    return matches[0]


download_locks = {}


@app.post("/api/download")
async def download(req: DownloadRequest):
    """Download a magnet, reusing an existing Real-Debrid torrent when possible."""
    if not release_is_allowed(req.release_name):
        raise HTTPException(422, "This release name is blocked by the configured source/codec rules")
    info_hash = magnet_info_hash(req.magnet)
    if not info_hash:
        return await download_locked(req, info_hash)
    lock = download_locks.setdefault(info_hash, asyncio.Lock())
    async with lock:
        return await download_locked(req, info_hash)


async def download_locked(req: DownloadRequest, info_hash: str | None):
    """Download a magnet explicitly supplied by the user."""
    if not TOKEN:
        raise HTTPException(503, "REAL_DEBRID_TOKEN is not configured")

    logger.info("download started media_type=%s title=%r", req.media_type, req.title)

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        existing = await find_existing_torrent(client, info_hash)
        if existing:
            torrent_id = existing["id"]
            logger.info("reusing existing RD torrent torrent_id=%s hash=%s status=%s",
                        torrent_id, info_hash, existing.get("status"))
        else:
            r = await rd_post(client, "/torrents/addMagnet", {"magnet": req.magnet})
            torrent_id = r.json()["id"]
            logger.info("Real-Debrid accepted new torrent_id=%s hash=%s",
                        torrent_id, info_hash)

        info = None
        for _ in range(180):
            info = (await rd_get(client, f"/torrents/info/{torrent_id}")).json()
            if info.get("files"):
                logger.info("torrent files ready torrent_id=%s count=%s",
                            torrent_id, len(info["files"]))
                break
            if info.get("status") in ("error", "dead", "virus", "magnet_error"):
                raise HTTPException(502, f"Real-Debrid failed: {info.get('status')}")
            await asyncio.sleep(POLL)

        if not info or not info.get("files"):
            raise HTTPException(504, "Real-Debrid did not expose torrent files")

        # Real-Debrid exposes torrent contents as individual files. Select only
        # playable video files so archives, samples, subtitles, and metadata
        # are never downloaded to the library.
        video_files = [
            f for f in info["files"]
            if Path(str(f.get("path", ""))).suffix.lower() in VIDEO_EXTENSIONS
        ]
        if not video_files:
            logger.warning(
                "torrent has no supported video files torrent_id=%s files=%s",
                torrent_id,
                [f.get("path") for f in info["files"]],
            )
            raise HTTPException(
                422,
                "This release has no standalone video files (it may contain only a RAR/ZIP archive). Choose another release.",
            )

        file_ids = ",".join(str(f["id"]) for f in video_files)
        logger.info("selecting video files torrent_id=%s count=%s",
                    torrent_id, len(video_files))
        if not existing or info.get("status") != "downloaded":
            await rd_post(client, f"/torrents/selectFiles/{torrent_id}",
                          {"files": file_ids})

        last_status = None
        for _ in range(720):
            info = (await rd_get(client, f"/torrents/info/{torrent_id}")).json()
            status = info.get("status")
            if status != last_status:
                logger.info("torrent status changed torrent_id=%s status=%s",
                            torrent_id, status)
                last_status = status
            if status == "downloaded":
                break
            if status in ("error", "dead", "virus", "magnet_error"):
                raise HTTPException(502, f"Real-Debrid torrent failed: {status}")
            await asyncio.sleep(POLL)
        else:
            raise HTTPException(504, "Timed out waiting for Real-Debrid")

        links = info.get("links", [])
        if not links:
            raise HTTPException(502, "Completed torrent returned no links")

        destination_root = ROOT / ("TV" if req.media_type == "tv" else "movies")
        if req.media_type == "tv":
            if req.season is None:
                raise HTTPException(422, "Season is required for TV downloads")
            show_dir = destination_root / safe_name(req.title or "Unknown show")
            target_dir = show_dir / f"S{req.season:02d}"
        else:
            target_dir = destination_root

        destination_root.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        for link in links:
            rr = await rd_post(client, "/unrestrict/link", {"link": link})
            payload = rr.json()
            direct = payload.get("download")
            if not direct:
                continue

            raw_filename = payload.get("filename") or "download.bin"
            if Path(raw_filename).suffix.lower() not in VIDEO_EXTENSIONS:
                logger.warning("skipping non-video RD link torrent_id=%s filename=%r",
                               torrent_id, raw_filename)
                continue

            filename = safe_name(raw_filename)
            target = target_dir / filename

            if target.exists():
                downloaded.append(str(target))
                continue

            await download_direct_link(client, direct, target)

            downloaded.append(str(target))

        result = {
            "torrent_id": torrent_id,
            "status": "downloaded",
            "destination": str(target_dir),
            "files": downloaded,
        }
        logger.info("download complete torrent_id=%s destination=%s files=%s",
                    torrent_id, target_dir, len(downloaded))
        return result


download_jobs = {}
download_tasks = {}


async def run_download_job(job_id: str, requests: list[DownloadRequest]):
    job = download_jobs[job_id]
    job["status"] = "running"
    logger.info("download batch started job_id=%s selections=%s",
                job_id, len(requests))
    semaphore = asyncio.Semaphore(3)

    async def process(index: int, request: DownloadRequest):
        item = job["items"][index]
        async with semaphore:
            item["status"] = "downloading"
            logger.info("batch item started job_id=%s item=%s title=%r",
                        job_id, index + 1, request.label or request.title)
            try:
                result = await download(request)
                item.update({"status": "complete", "result": result})
                job["completed"] += 1
                logger.info("batch item completed job_id=%s item=%s torrent_id=%s",
                            job_id, index + 1, result.get("torrent_id"))
            except HTTPException as exc:
                item.update({"status": "failed", "error": exc.detail})
                job["failed"] += 1
                logger.error("batch item failed job_id=%s item=%s error=%s",
                             job_id, index + 1, exc.detail)
            except Exception:
                item.update({"status": "failed", "error": "Unexpected download error"})
                job["failed"] += 1
                logger.exception("batch item failed unexpectedly job_id=%s item=%s",
                                 job_id, index + 1)

    await asyncio.gather(*(process(index, request) for index, request in enumerate(requests)))
    job["status"] = "complete"
    logger.info("download batch complete job_id=%s completed=%s failed=%s",
                job_id, job["completed"], job["failed"])


@app.post("/api/download-batches", status_code=202)
async def create_download_batch(batch: DownloadBatchRequest):
    if not TOKEN:
        raise HTTPException(503, "REAL_DEBRID_TOKEN is not configured")

    job_id = uuid.uuid4().hex
    download_jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "total": len(batch.downloads),
        "completed": 0,
        "failed": 0,
        "items": [
            {
                "index": i,
                "title": item.label or item.title or f"Selection {i + 1}",
                "selection_key": item.selection_key,
                "status": "queued",
            }
            for i, item in enumerate(batch.downloads)
        ],
    }
    download_tasks[job_id] = asyncio.create_task(
        run_download_job(job_id, batch.downloads)
    )
    logger.info("download batch queued job_id=%s selections=%s",
                job_id, len(batch.downloads))
    return {"job_id": job_id, "status": "queued", "total": len(batch.downloads)}


@app.get("/api/download-batches/{job_id}")
async def get_download_batch(job_id: str):
    job = download_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Download batch not found")
    active = sum(item["status"] == "downloading" for item in job["items"])
    return {**job, "active": active}
