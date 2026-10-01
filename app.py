import asyncio
import base64
import binascii
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

RD_BASE = "https://api.real-debrid.com/rest/1.0"
TOKEN = os.environ.get("REAL_DEBRID_TOKEN")
ROOT = Path(os.environ.get("DOWNLOAD_ROOT", "/media"))
POLL = int(os.environ.get("POLL_SECONDS", "10"))
TORRENTIO_URL = os.environ.get("TORRENTIO_URL", "").rstrip("/")
RD_REQUEST_ATTEMPTS = 3
RETRYABLE_HTTP_STATUSES = {408, 425, 429}
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


class DownloadRequest(BaseModel):
    magnet: str = Field(min_length=10)
    media_type: Literal["tv", "movies"]
    title: str | None = None
    season: int | None = Field(default=None, ge=1)
    label: str | None = None
    selection_key: str | None = None


class DownloadBatchRequest(BaseModel):
    downloads: list[DownloadRequest] = Field(min_length=1, max_length=30)


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
async def index():
    return FileResponse("/app/static/index.html")


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
