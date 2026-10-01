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
DOWNLOAD_DB = DATA_DIR / "downloads.sqlite3"
AUTH_STATE_FILE = DATA_DIR / "auth.json"
AUTH_USERS_FILE = DATA_DIR / "users.json"
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


def persist_auth_users():
    saved = {
        username: {
            "salt": user["salt"].hex(),
            "password_hash": user["password_hash"].hex(),
            "role": user["role"],
        }
        for username, user in AUTH_USERS.items()
    }
    temporary = AUTH_USERS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(saved))
    temporary.replace(AUTH_USERS_FILE)


def initialize_auth():
    if AUTH_USERS_FILE.exists():
        saved_users = json.loads(AUTH_USERS_FILE.read_text())
        users = {
            username: {
                "salt": bytes.fromhex(value["salt"]),
                "password_hash": bytes.fromhex(value["password_hash"]),
                "role": value["role"],
            }
            for username, value in saved_users.items()
        }
        changed = False
        admin_password = os.environ.get("APP_PASSWORD")
        if admin_password:
            admin_username = os.environ.get("APP_USERNAME") or "admin"
            salt = secrets.token_bytes(16)
            users[admin_username] = {
                "salt": salt,
                "password_hash": password_hash(admin_password, salt),
                "role": "admin",
            }
            changed = True
        requestor_password = os.environ.get("APP_REQUESTOR_PASSWORD")
        if requestor_password and "requestor" not in users:
            salt = secrets.token_bytes(16)
            users["requestor"] = {
                "salt": salt,
                "password_hash": password_hash(requestor_password, salt),
                "role": "requestor",
            }
            changed = True
        if changed:
            AUTH_USERS_FILE.write_text(json.dumps({
                username: {"salt": user["salt"].hex(), "password_hash": user["password_hash"].hex(), "role": user["role"]}
                for username, user in users.items()
            }))
        return users

    configured_password = os.environ.get("APP_PASSWORD")
    configured_username = os.environ.get("APP_USERNAME")
    if configured_password:
        username = configured_username or "admin"
        salt = secrets.token_bytes(16)
        users = {username: {"salt": salt, "password_hash": password_hash(configured_password, salt), "role": "admin"}}
    elif AUTH_STATE_FILE.exists():
        saved = json.loads(AUTH_STATE_FILE.read_text())
        username = saved["username"]
        users = {username: {"salt": bytes.fromhex(saved["salt"]),
                            "password_hash": bytes.fromhex(saved["password_hash"]), "role": "admin"}}
    else:
        username = configured_username or "admin"
        initial_password = secrets.token_urlsafe(18)
        salt = secrets.token_bytes(16)
        users = {username: {"salt": salt, "password_hash": password_hash(initial_password, salt), "role": "admin"}}
        logger.warning("Initial app login created username=%s password=%s; set APP_PASSWORD to replace it",
                       username, initial_password)

    AUTH_USERS_FILE.write_text(json.dumps({
        username: {"salt": user["salt"].hex(), "password_hash": user["password_hash"].hex(), "role": user["role"]}
        for username, user in users.items()
    }))
    return users


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


AUTH_USERS = initialize_auth()
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


def init_download_db():
    with sqlite3.connect(DOWNLOAD_DB) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS download_batches (
            job_id TEXT PRIMARY KEY,
            job_json TEXT NOT NULL,
            requests_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")


init_download_db()

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
        if int(expires) < int(time.time()) or username not in AUTH_USERS:
            return None
        return username
    except (ValueError, UnicodeDecodeError):
        return None


@app.middleware("http")
async def require_login(request: Request, call_next):
    username = session_username(request.cookies.get(SESSION_COOKIE))
    request.state.username = username
    request.state.role = AUTH_USERS[username]["role"] if username else None
    if request.url.path.startswith("/api/") and request.url.path not in {
        "/api/login", "/api/session",
    } and not username:
        return JSONResponse({"detail": "Login required"}, status_code=401)
    requestor_routes = {
        "/api/session", "/api/logout", "/api/title-search", "/api/requests",
        "/api/discover", "/api/discover-season", "/api/discover-movie", "/api/catalog",
    }
    if username and request.state.role == "requestor" and request.url.path.startswith("/api/"):
        allowed = request.url.path in requestor_routes
        if request.url.path.startswith("/api/requests/") and request.method == "GET":
            allowed = True
        if not allowed:
            return JSONResponse({"detail": "This account can only submit and view media requests"}, status_code=403)
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


class RequestorAccountInput(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=200)


class RequestorPasswordInput(BaseModel):
    password: str = Field(min_length=8, max_length=200)


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


def download_target_dir(req: DownloadRequest) -> Path:
    destination_root = ROOT / ("TV" if req.media_type == "tv" else "Movies")
    if req.media_type == "tv":
        if req.season is None:
            raise HTTPException(422, "Season is required for TV downloads")
        return destination_root / safe_name(req.title or "Unknown show") / f"S{req.season:02d}"
    return destination_root


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
    return {"authenticated": bool(request.state.username), "username": request.state.username,
            "role": request.state.role}


@app.post("/api/login")
async def login(payload: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    failures, retry_after = LOGIN_FAILURES.get(client_ip, (0, 0))
    if retry_after > time.time():
        raise HTTPException(429, "Too many login attempts. Try again shortly.")
    user = AUTH_USERS.get(payload.username)
    valid_password = False
    if user:
        candidate_hash = password_hash(payload.password, user["salt"])
        valid_password = hmac.compare_digest(candidate_hash, user["password_hash"])
    if not user or not valid_password:
        failures += 1
        LOGIN_FAILURES[client_ip] = (0, time.time() + 60) if failures >= 5 else (failures, 0)
        raise HTTPException(401, "Incorrect username or password")
    LOGIN_FAILURES.pop(client_ip, None)
    response = JSONResponse({"authenticated": True, "username": payload.username,
                             "role": user["role"]})
    response.set_cookie(
        SESSION_COOKIE,
        make_session(payload.username),
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


def require_admin(request: Request):
    if request.state.role != "admin":
        raise HTTPException(403, "Admin access required")


@app.get("/api/accounts")
async def list_requestor_accounts(request: Request):
    require_admin(request)
    return {"accounts": sorted(
        ({"username": username, "role": user["role"]}
         for username, user in AUTH_USERS.items() if user["role"] == "requestor"),
        key=lambda user: user["username"].casefold(),
    )}


@app.post("/api/accounts", status_code=201)
async def create_requestor_account(payload: RequestorAccountInput, request: Request):
    require_admin(request)
    if payload.username in AUTH_USERS:
        raise HTTPException(409, "That username is already in use")
    salt = secrets.token_bytes(16)
    AUTH_USERS[payload.username] = {
        "salt": salt,
        "password_hash": password_hash(payload.password, salt),
        "role": "requestor",
    }
    persist_auth_users()
    logger.info("requestor account created username=%s by=%s", payload.username, request.state.username)
    return {"username": payload.username, "role": "requestor"}


@app.patch("/api/accounts/{username}")
async def reset_requestor_password(
    username: str, payload: RequestorPasswordInput, request: Request
):
    require_admin(request)
    user = AUTH_USERS.get(username)
    if not user or user["role"] != "requestor":
        raise HTTPException(404, "Requestor account not found")
    salt = secrets.token_bytes(16)
    user["salt"] = salt
    user["password_hash"] = password_hash(payload.password, salt)
    persist_auth_users()
    logger.info("requestor password reset username=%s by=%s", username, request.state.username)
    return {"username": username, "updated": True}


@app.delete("/api/accounts/{username}", status_code=204)
async def delete_requestor_account(username: str, request: Request):
    require_admin(request)
    user = AUTH_USERS.get(username)
    if not user or user["role"] != "requestor":
        raise HTTPException(404, "Requestor account not found")
    del AUTH_USERS[username]
    persist_auth_users()
    logger.info("requestor account deleted username=%s by=%s", username, request.state.username)
    return Response(status_code=204)


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


def media_video_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.rglob("*")
         if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda path: str(path).casefold(),
    )


def normalize_catalog_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.casefold())


def movie_title_from_path(path: Path) -> tuple[str, str | None]:
    label = re.sub(r"[._]+", " ", path.stem).strip()
    year_match = re.search(r"\b(?:19|20)\d{2}\b", label)
    year = year_match.group(0) if year_match else None
    title = label[:year_match.start()].strip(" -._()[]") if year_match else label
    return title or label, year


def catalog_movie_match(title: str) -> bool:
    wanted = normalize_catalog_title(title)
    for folder in (ROOT / "Movies", ROOT / "movies"):
        if any(normalize_catalog_title(movie_title_from_path(path)[0]) == wanted
               for path in media_video_files(folder)):
            return True
    return False


def catalog_series_match(title: str, season_number: int) -> dict:
    wanted = normalize_catalog_title(title)
    downloaded_seasons = set()
    show_found = False
    tv_root = ROOT / "TV"
    if tv_root.is_dir():
        for show_dir in tv_root.iterdir():
            if not show_dir.is_dir() or normalize_catalog_title(show_dir.name) != wanted:
                continue
            show_found = True
            for season_dir in show_dir.iterdir():
                if not season_dir.is_dir() or not media_video_files(season_dir):
                    continue
                match = re.match(r"^(?:s|season\s*)0*(\d+)$", season_dir.name, re.I)
                if match:
                    downloaded_seasons.add(int(match.group(1)))
    return {
        "show_in_catalog": show_found,
        "already_downloaded": season_number in downloaded_seasons,
        "downloaded_seasons": [f"S{number:02d}" for number in sorted(downloaded_seasons)],
    }


@app.get("/api/catalog")
async def media_catalog():
    movies = []
    movie_roots = [ROOT / "Movies", ROOT / "movies"]
    seen_movies = set()
    for movie_root in movie_roots:
        for path in media_video_files(movie_root):
            if path in seen_movies:
                continue
            seen_movies.add(path)
            title, year = movie_title_from_path(path)
            movies.append({
                "title": title,
                "year": year,
                "file": path.name,
            })

    shows = []
    tv_root = ROOT / "TV"
    if tv_root.is_dir():
        for show_dir in sorted((p for p in tv_root.iterdir() if p.is_dir()),
                               key=lambda path: path.name.casefold()):
            seasons = []
            for season_dir in sorted((p for p in show_dir.iterdir() if p.is_dir()),
                                     key=lambda path: path.name.casefold()):
                episode_count = len(media_video_files(season_dir))
                if episode_count:
                    seasons.append({"name": season_dir.name, "episodes": episode_count})
            unsorted_count = sum(
                1 for path in show_dir.iterdir()
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
            )
            if unsorted_count:
                seasons.append({"name": "Unsorted", "episodes": unsorted_count})
            if seasons:
                shows.append({"title": show_dir.name, "seasons": seasons})

    return {
        "movies": movies,
        "movie_count": len(movies),
        "shows": shows,
        "show_count": len(shows),
        "episode_count": sum(season["episodes"] for show in shows for season in show["seasons"]),
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
    series_title = meta.get("name") or imdb_id
    return {
        "imdb_id": imdb_id,
        "series_title": series_title,
        "season": season,
        "episodes": episodes,
        **catalog_series_match(series_title, season),
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
        "already_downloaded": catalog_movie_match(meta.get("name") or imdb_id),
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


async def download_direct_link(client, url, target, progress_callback=None):
    if progress_callback:
        progress_callback("Downloading video file", 0, f"{target.name} · starting")
    try:
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
                    total_bytes = int(response.headers.get("content-length") or 0)
                    downloaded_bytes = 0
                    last_report = 0.0
                    with target.open("wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            output.write(chunk)
                            downloaded_bytes += len(chunk)
                            now = time.monotonic()
                            if progress_callback and (now - last_report >= 2 or
                                                      total_bytes and downloaded_bytes >= total_bytes):
                                percent = round(downloaded_bytes * 100 / total_bytes, 1) if total_bytes else None
                                progress_callback(
                                    "Downloading video file", percent,
                                    f"{target.name} · {downloaded_bytes // (1024 * 1024)} MB" +
                                    (f" of {total_bytes // (1024 * 1024)} MB" if total_bytes else ""),
                                )
                                last_report = now
                return
            except httpx.RequestError as exc:
                if attempt == RD_REQUEST_ATTEMPTS:
                    raise HTTPException(
                        502, f"File download failed after {attempt} attempts"
                    ) from exc
                logger.warning("direct download network error attempt=%s/%s",
                               attempt, RD_REQUEST_ATTEMPTS)
                await asyncio.sleep(attempt)
    except asyncio.CancelledError:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove partial file after cancellation path=%s", target)
        raise


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
async def download(req: DownloadRequest, progress_callback=None):
    """Download a magnet, reusing an existing Real-Debrid torrent when possible."""
    if not release_is_allowed(req.release_name):
        raise HTTPException(422, "This release name is blocked by the configured source/codec rules")
    info_hash = magnet_info_hash(req.magnet)
    if not info_hash:
        return await download_locked(req, info_hash, progress_callback)
    lock = download_locks.setdefault(info_hash, asyncio.Lock())
    async with lock:
        return await download_locked(req, info_hash, progress_callback)


async def download_locked(req: DownloadRequest, info_hash: str | None, progress_callback=None):
    """Download a magnet explicitly supplied by the user."""
    if not TOKEN:
        raise HTTPException(503, "REAL_DEBRID_TOKEN is not configured")

    logger.info("download started media_type=%s title=%r", req.media_type, req.title)
    if progress_callback:
        progress_callback("Checking Real-Debrid", None, "Looking for an existing torrent")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        existing = await find_existing_torrent(client, info_hash)
        if existing:
            torrent_id = existing["id"]
            logger.info("reusing existing RD torrent torrent_id=%s hash=%s status=%s",
                        torrent_id, info_hash, existing.get("status"))
            if progress_callback:
                progress_callback("Reusing existing torrent", existing.get("progress"),
                                  f"Real-Debrid status: {existing.get('status', 'unknown')}")
        else:
            if progress_callback:
                progress_callback("Adding magnet to Real-Debrid", 0, "Waiting for torrent details")
            r = await rd_post(client, "/torrents/addMagnet", {"magnet": req.magnet})
            torrent_id = r.json()["id"]
            logger.info("Real-Debrid accepted new torrent_id=%s hash=%s",
                        torrent_id, info_hash)

        info = None
        for _ in range(180):
            info = (await rd_get(client, f"/torrents/info/{torrent_id}")).json()
            if info.get("files"):
                if progress_callback:
                    progress_callback("Torrent details ready", info.get("progress"),
                                      f"{len(info['files'])} files found")
                logger.info("torrent files ready torrent_id=%s count=%s",
                            torrent_id, len(info["files"]))
                break
            if info.get("status") in ("error", "dead", "virus", "magnet_error"):
                raise HTTPException(502, f"Real-Debrid failed: {info.get('status')}")
            if progress_callback:
                progress_callback("Waiting for torrent details", info.get("progress"),
                                  f"Real-Debrid status: {info.get('status', 'unknown')}")
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
        if progress_callback:
            progress_callback("Selecting video files", info.get("progress"),
                              f"{len(video_files)} video file(s) selected")
        logger.info("selecting video files torrent_id=%s count=%s",
                    torrent_id, len(video_files))
        if not existing or info.get("status") != "downloaded":
            await rd_post(client, f"/torrents/selectFiles/{torrent_id}",
                          {"files": file_ids})

        last_status = None
        for _ in range(720):
            info = (await rd_get(client, f"/torrents/info/{torrent_id}")).json()
            status = info.get("status")
            if progress_callback:
                percent = info.get("progress")
                status_text = str(status or "waiting").replace("_", " ").capitalize()
                progress_callback(f"Real-Debrid: {status_text}", percent,
                                  f"Torrent status: {status_text}")
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

        target_dir = download_target_dir(req)
        destination_root = ROOT / ("TV" if req.media_type == "tv" else "Movies")
        destination_root.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        skipped_links = []
        for link in links:
            if progress_callback:
                progress_callback("Preparing video file", 0, "Getting a direct download link")
            try:
                rr = await rd_post(client, "/unrestrict/link", {"link": link})
            except HTTPException as exc:
                error_detail = str(exc.detail).lower()
                if "infringing_file" in error_detail or "error_code\": 35" in error_detail:
                    logger.warning("Real-Debrid rejected one file as infringing torrent_id=%s",
                                   torrent_id)
                    skipped_links.append("Real-Debrid blocked one file; other files were kept")
                    continue
                raise
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

            await download_direct_link(client, direct, target, progress_callback)

            downloaded.append(str(target))

        if skipped_links and not downloaded:
            raise HTTPException(502, "Real-Debrid blocked all selected files as infringing")

        result = {
            "torrent_id": torrent_id,
            "status": "downloaded",
            "destination": str(target_dir),
            "files": downloaded,
            "warnings": skipped_links,
        }
        logger.info("download complete torrent_id=%s destination=%s files=%s",
                    torrent_id, target_dir, len(downloaded))
        return result


download_jobs = {}
download_requests = {}
download_tasks = {}


def save_download_job(job_id: str):
    job = download_jobs[job_id]
    job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with sqlite3.connect(DOWNLOAD_DB) as db:
        db.execute(
            "INSERT INTO download_batches (job_id, job_json, requests_json, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
            "job_json=excluded.job_json, requests_json=excluded.requests_json, updated_at=excluded.updated_at",
            (job_id, json.dumps(job),
             json.dumps([item.model_dump() for item in download_requests[job_id]]),
             job["updated_at"]),
        )


@app.on_event("startup")
async def restore_download_jobs():
    with sqlite3.connect(DOWNLOAD_DB) as db:
        rows = db.execute(
            "SELECT job_id, job_json, requests_json FROM download_batches "
            "ORDER BY updated_at DESC"
        ).fetchall()
    for job_id, job_json, requests_json in rows:
        job = json.loads(job_json)
        requests = [DownloadRequest(**item) for item in json.loads(requests_json)]
        download_jobs[job_id] = job
        download_requests[job_id] = requests
        unfinished = False
        for item in job["items"]:
            if item["status"] == "downloading":
                item["status"] = "queued"
            if item["status"] == "queued":
                unfinished = True
        if unfinished:
            job["status"] = "queued"
            save_download_job(job_id)
            download_tasks[job_id] = asyncio.create_task(run_download_job(job_id, requests))
        elif job["status"] != "complete":
            job["status"] = "complete"
            save_download_job(job_id)


async def run_download_job(job_id: str, requests: list[DownloadRequest]):
    job = download_jobs[job_id]
    job["status"] = "running"
    save_download_job(job_id)
    logger.info("download batch started job_id=%s selections=%s",
                job_id, len(requests))
    semaphore = asyncio.Semaphore(3)

    async def process(index: int, request: DownloadRequest):
        item = job["items"][index]
        if item["status"] in {"complete", "failed"}:
            return
        async with semaphore:
            item["status"] = "downloading"
            item["stage"] = "Starting download"
            item["progress"] = 0
            item["detail"] = "Preparing Real-Debrid request"
            save_download_job(job_id)
            logger.info("batch item started job_id=%s item=%s title=%r",
                        job_id, index + 1, request.label or request.title)

            def report_progress(stage, progress=None, detail=None):
                item["stage"] = stage
                item["progress"] = progress
                if detail is not None:
                    item["detail"] = detail
                save_download_job(job_id)

            try:
                result = await download(request, report_progress)
                warnings = result.get("warnings") or []
                item.update({
                    "status": "complete", "stage": "Complete", "progress": 100,
                    "detail": result.get("destination", "Download complete"),
                    "warnings": warnings, "result": result,
                })
                job["completed"] += 1
                save_download_job(job_id)
                logger.info("batch item completed job_id=%s item=%s torrent_id=%s",
                            job_id, index + 1, result.get("torrent_id"))
            except HTTPException as exc:
                item.update({"status": "failed", "stage": "Failed", "error": exc.detail,
                             "detail": exc.detail})
                job["failed"] += 1
                save_download_job(job_id)
                logger.error("batch item failed job_id=%s item=%s error=%s",
                             job_id, index + 1, exc.detail)
            except Exception:
                item.update({"status": "failed", "stage": "Failed",
                             "error": "Unexpected download error",
                             "detail": "Unexpected download error"})
                job["failed"] += 1
                save_download_job(job_id)
                logger.exception("batch item failed unexpectedly job_id=%s item=%s",
                                 job_id, index + 1)

    await asyncio.gather(*(process(index, request) for index, request in enumerate(requests)))
    job["status"] = "complete"
    save_download_job(job_id)
    logger.info("download batch complete job_id=%s completed=%s failed=%s",
                job_id, job["completed"], job["failed"])


@app.post("/api/download-batches", status_code=202)
async def create_download_batch(batch: DownloadBatchRequest):
    if not TOKEN:
        raise HTTPException(503, "REAL_DEBRID_TOKEN is not configured")

    job_id = uuid.uuid4().hex
    download_requests[job_id] = batch.downloads
    download_jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "total": len(batch.downloads),
        "completed": 0,
        "failed": 0,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "items": [
            {
                "index": i,
                "title": item.label or item.title or f"Selection {i + 1}",
                "selection_key": item.selection_key,
                "status": "queued",
                "stage": "Queued",
                "progress": 0,
                "detail": "Waiting to start",
            }
            for i, item in enumerate(batch.downloads)
        ],
    }
    save_download_job(job_id)
    download_tasks[job_id] = asyncio.create_task(
        run_download_job(job_id, batch.downloads)
    )
    logger.info("download batch queued job_id=%s selections=%s",
                job_id, len(batch.downloads))
    return {"job_id": job_id, "status": "queued", "total": len(batch.downloads)}


@app.get("/api/download-batches")
async def list_download_batches():
    jobs = sorted(download_jobs.values(), key=lambda job: job.get("created_at", ""), reverse=True)
    return {"jobs": [
        {**job, "active": sum(item["status"] == "downloading" for item in job["items"])}
        for job in jobs[:500]
    ]}


@app.post("/api/downloads/stop-all")
async def stop_all_downloads(request: Request):
    require_admin(request)
    pending_jobs = {
        job_id: job for job_id, job in download_jobs.items()
        if any(item.get("status") in {"queued", "downloading"}
               for item in job.get("items", []))
    }
    tasks = []
    for job_id in pending_jobs:
        task = download_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    stopped = 0
    for job_id, job in pending_jobs.items():
        stopped_in_job = 0
        for item in job.get("items", []):
            if item.get("status") not in {"queued", "downloading"}:
                continue
            item.update({
                "status": "cancelled",
                "stage": "Stopped",
                "detail": "Stopped by admin",
                "progress": None,
            })
            item.pop("error", None)
            stopped += 1
            stopped_in_job += 1
        job["cancelled"] = job.get("cancelled", 0) + stopped_in_job
        job["status"] = "complete"
        save_download_job(job_id)
        download_tasks.pop(job_id, None)
    logger.warning("active downloads stopped by admin username=%s selections=%s",
                   request.state.username, stopped)
    return {"stopped": stopped}


@app.delete("/api/downloads/history")
async def clear_download_history(request: Request):
    require_admin(request)
    removable = [
        job_id for job_id, job in download_jobs.items()
        if all(item.get("status") not in {"queued", "downloading"}
               for item in job.get("items", []))
    ]
    if removable:
        with sqlite3.connect(DOWNLOAD_DB) as db:
            db.executemany(
                "DELETE FROM download_batches WHERE job_id=?",
                ((job_id,) for job_id in removable),
            )
        for job_id in removable:
            download_jobs.pop(job_id, None)
            download_requests.pop(job_id, None)
            download_tasks.pop(job_id, None)
    logger.info("download history cleared by admin username=%s batches=%s",
                request.state.username, len(removable))
    return {"cleared": len(removable)}


def destination_folder_info(path: Path, current_files: set[str] | None = None) -> dict:
    current_files = current_files or set()
    files = []
    if path.is_dir():
        for file_path in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
            if not file_path.is_file():
                continue
            try:
                size = file_path.stat().st_size
            except OSError:
                size = None
            files.append({
                "name": file_path.name,
                "size": size,
                "current": file_path.name.casefold() in current_files,
            })
    return {"path": str(path), "files": files}


@app.get("/api/downloads/current")
async def current_downloads():
    active = []
    folder_requests: dict[str, tuple[Path, set[str]]] = {}
    for job_id, job in download_jobs.items():
        requests = download_requests.get(job_id, [])
        for index, item in enumerate(job.get("items", [])):
            if item.get("status") != "downloading" or index >= len(requests):
                continue
            req = requests[index]
            destination = download_target_dir(req)
            detail = item.get("detail") or ""
            current_file = detail.split(" · ", 1)[0] if item.get("stage") == "Downloading video file" else ""
            active.append({
                "title": item.get("title") or req.title or "Untitled download",
                "file": current_file,
                "stage": item.get("stage") or "Preparing download",
                "progress": item.get("progress"),
                "detail": detail,
                "destination": str(destination),
            })
            entry = folder_requests.setdefault(str(destination), (destination, set()))
            if current_file:
                entry[1].add(current_file.casefold())

    if not active:
        for job_id, job in sorted(
            download_jobs.items(),
            key=lambda pair: pair[1].get("created_at", ""),
            reverse=True,
        ):
            requests = download_requests.get(job_id, [])
            candidates = [
                (item, requests[index]) for index, item in enumerate(job.get("items", []))
                if index < len(requests) and item.get("status") in {"complete", "failed", "cancelled"}
            ]
            candidates.sort(key=lambda pair: pair[0].get("status") == "complete", reverse=True)
            if candidates:
                req = candidates[0][1]
                destination = download_target_dir(req)
                if destination.is_dir():
                    folder_requests[str(destination)] = (destination, set())
                    break

    return {
        "active_count": len(active),
        "pending_count": sum(
            item.get("status") in {"queued", "downloading"}
            for job in download_jobs.values()
            for item in job.get("items", [])
        ),
        "downloads": active,
        "folders": [
            destination_folder_info(path, current_files)
            for path, current_files in folder_requests.values()
        ],
    }


@app.get("/api/download-batches/{job_id}")
async def get_download_batch(job_id: str):
    job = download_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Download batch not found")
    active = sum(item["status"] == "downloading" for item in job["items"])
    return {**job, "active": active}
