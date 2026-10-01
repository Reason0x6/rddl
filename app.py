import asyncio
import os
import re
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

app = FastAPI(title="RD Media Downloader", version="2.0.0")


class DownloadRequest(BaseModel):
    magnet: str = Field(min_length=10)
    media_type: Literal["tv", "movies"]
    title: str | None = None


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\\\|?*\\x00-\\x1f]', "_", value)
    value = re.sub(r"\\s+", " ", value).strip().rstrip(".")
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

    results.sort(key=lambda x: (x["seeders"], x["resolution"] or ""), reverse=True)
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


# ---- Existing user-selected RD download workflow ----

async def rd_post(client, path, data=None):
    r = await client.post(RD_BASE + path, headers=rd_headers(), data=data)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Real-Debrid error: {r.text[:1000]}")
    return r


async def rd_get(client, path):
    r = await client.get(RD_BASE + path, headers=rd_headers())
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Real-Debrid error: {r.text[:1000]}")
    return r


@app.post("/api/download")
async def download(req: DownloadRequest):
    """Download a magnet explicitly supplied by the user."""
    if not TOKEN:
        raise HTTPException(503, "REAL_DEBRID_TOKEN is not configured")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await rd_post(client, "/torrents/addMagnet", {"magnet": req.magnet})
        torrent_id = r.json()["id"]

        info = None
        for _ in range(180):
            info = (await rd_get(client, f"/torrents/info/{torrent_id}")).json()
            if info.get("files"):
                break
            if info.get("status") in ("error", "dead", "virus", "magnet_error"):
                raise HTTPException(502, f"Real-Debrid failed: {info.get('status')}")
            await asyncio.sleep(POLL)

        if not info or not info.get("files"):
            raise HTTPException(504, "Real-Debrid did not expose torrent files")

        file_ids = ",".join(str(f["id"]) for f in info["files"])
        await rd_post(client, f"/torrents/selectFiles/{torrent_id}",
                      {"files": file_ids})

        for _ in range(720):
            info = (await rd_get(client, f"/torrents/info/{torrent_id}")).json()
            status = info.get("status")
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

        destination_root = ROOT / ("tv" if req.media_type == "tv" else "movies")
        destination_root.mkdir(parents=True, exist_ok=True)

        target_dir = destination_root / safe_name(
            req.title or info.get("filename", "download")
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        for link in links:
            rr = await rd_post(client, "/unrestrict/link", {"link": link})
            payload = rr.json()
            direct = payload.get("download")
            if not direct:
                continue

            filename = safe_name(payload.get("filename") or "download.bin")
            target = target_dir / filename

            if target.exists():
                downloaded.append(str(target))
                continue

            async with client.stream("GET", direct) as dr:
                dr.raise_for_status()
                with target.open("wb") as out:
                    async for chunk in dr.aiter_bytes(1024 * 1024):
                        out.write(chunk)

            downloaded.append(str(target))

        return {
            "torrent_id": torrent_id,
            "status": "downloaded",
            "destination": str(target_dir),
            "files": downloaded,
        }
