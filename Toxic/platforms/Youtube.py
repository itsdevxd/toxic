import asyncio
import os
import random
import re
import time
import uuid
import aiofiles
import httpx
import yt_dlp
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union, List
from urllib.parse import unquote

from pyrogram import errors
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from youtubesearchpython.__future__ import VideosSearch
from Toxic import app
from Toxic.logging import LOGGER
from Toxic.utils.formatters import time_to_seconds
from config import API_URL, API_KEY, DOWNLOADS_DIR

# ---------- Configuration ----------
PROXY_LIST_ENV = os.getenv("YTDL_PROXIES", "")
SERVER_RELAY = os.getenv("https://toxicapi.vercel.app/api/proxy?url=", "")
MAX_NETWORK_RETRIES = int(os.getenv("YTDL_MAX_RETRIES", "3"))
HTTPX_TIMEOUT = int(os.getenv("YTDL_HTTP_TIMEOUT", "90"))
# ---------- End config ----------

@dataclass
class DownloadResult:
    success: bool
    file_path: Optional[Path] = None
    error: Optional[str] = None

class YouTubeAPI:
    DEFAULT_TIMEOUT = 120
    DEFAULT_DOWNLOAD_TIMEOUT = 120
    CHUNK_SIZE = 8192
    MAX_RETRIES = 2
    BACKOFF_FACTOR = 1.0
    BASE_URL = "https://www.youtube.com/watch?v="
    PLAYLIST_BASE = "https://youtube.com/playlist?list="
    REGEX = r"(?:youtube\.com|youtu\.be)"
    STATUS_URL = "https://www.youtube.com/oembed?url="

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, download_timeout: int = DEFAULT_DOWNLOAD_TIMEOUT, max_redirects: int = 0):
        self._timeout = timeout
        self._download_timeout = download_timeout
        self._max_redirects = max_redirects
        self._session = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=self._timeout, read=self._timeout, write=self._timeout, pool=self._timeout),
            follow_redirects=max_redirects > 0,
            max_redirects=max_redirects,
        )
        self._regex = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        self._proxies = self._parse_proxies(PROXY_LIST_ENV)

    async def close(self) -> None:
        try:
            await self._session.aclose()
        except Exception as e:
            LOGGER(__name__).error("Error closing HTTP session: %s", repr(e))

    @staticmethod
    def _parse_proxies(proxy_env: str) -> List[str]:
        if not proxy_env:
            return []
        return [p.strip() for p in proxy_env.split(",") if p.strip()]

    def _choose_proxy(self) -> Optional[str]:
        if not self._proxies:
            return None
        return random.choice(self._proxies)

    def _get_headers(self, url: str, base_headers: dict[str, str]) -> dict[str, str]:
        headers = base_headers.copy()
        if API_URL and url.startswith(API_URL):
            headers["X-API-Key"] = API_KEY
        headers.setdefault("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36")
        return headers

    # -------------------------
    # File Download
    # -------------------------
    async def download_file(self, url: str, file_path: Optional[Union[str, Path]] = None, overwrite: bool = False, use_proxy: Optional[str] = None, **kwargs: Any) -> DownloadResult:
        if not url:
            return DownloadResult(success=False, error="Empty URL provided")
        headers = self._get_headers(url, kwargs.pop("headers", {}))
        proxies = {"all://": use_proxy} if use_proxy else None
        for attempt in range(MAX_NETWORK_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT, proxies=proxies) as client:
                    async with client.stream("GET", url, timeout=self._download_timeout, headers=headers) as response:
                        response.raise_for_status()
                        if file_path is None:
                            cd = response.headers.get("Content-Disposition", "")
                            match = re.search(r'filename="?([^"]+)"?', cd)
                            filename = unquote(match[1]) if match else (Path(url).name or uuid.uuid4().hex)
                            path = Path(DOWNLOADS_DIR) / filename
                        else:
                            path = Path(file_path) if isinstance(file_path, str) else file_path
                        if path.exists() and not overwrite:
                            return DownloadResult(success=True, file_path=path)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        async with aiofiles.open(path, "wb") as f:
                            async for chunk in response.aiter_bytes(self.CHUNK_SIZE):
                                await f.write(chunk)
                        return DownloadResult(success=True, file_path=path)
            except Exception as e:
                last_err = repr(e)
            await asyncio.sleep(1 + attempt * 2)
        return DownloadResult(success=False, error=f"Failed to download {url}: {last_err}")

    # -------------------------
    # API Download
    # -------------------------
    async def download_with_api(self, video_id: str, is_video: bool = False) -> Optional[Path]:
        if not API_URL or not API_KEY:
            return None
        if not video_id:
            return None
        public_url = await self.make_request(f"{API_URL}/yt?id={video_id}&video={is_video}")
        if not public_url:
            return None
        dl_url = public_url.get("results")
        if not dl_url:
            return None
        if not re.match(r"https:\/\/t\.me\/(?:[a-zA-Z0-9_]{5,}|c\/\d+)\/(\d+)", dl_url):
            dl = await self.download_file(dl_url)
            return dl.file_path if dl.success else None
        try:
            match = re.match(r"https:\/\/t\.me\/([a-zA-Z0-9_]{5,}|c\/\d+)\/(\d+)", dl_url)
            if not match:
                return None
            chat_id, message_id = match.groups()
            if chat_id.startswith("c/"):
                chat_id = f"-100{chat_id[2:]}"
            msg = await app.get_messages(chat_id=chat_id, message_ids=int(message_id))
            if not msg:
                return None
            path = await msg.download()
            return Path(path) if path else None
        except errors.FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return await self.download_with_api(video_id, is_video)
        except Exception:
            return None

    # -------------------------
    # YouTube Info
    # -------------------------
    async def details(self, link: str, videoid: Union[bool, str] = None) -> dict:
        if videoid:
            link = self.BASE_URL + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        result = (await results.next())["result"][0]
        duration_min = result["duration"]
        duration_sec = 0 if str(duration_min) == "None" else int(time_to_seconds(duration_min))
        return {
            "title": result["title"],
            "duration_min": duration_min,
            "duration_sec": duration_sec,
            "thumbnail": result["thumbnails"][0]["url"].split("?")[0],
            "id": result["id"],
        }

    async def title(self, link: str, videoid: Union[bool, str] = None) -> str:
        return (await self.details(link, videoid))["title"]

    async def duration(self, link: str, videoid: Union[bool, str] = None) -> str:
        return (await self.details(link, videoid))["duration_min"]

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None) -> str:
        return (await self.details(link, videoid))["thumbnail"]

    async def track(self, link: str, videoid: Union[bool, str] = None) -> tuple[dict, str]:
        info = await self.details(link, videoid)
        return info, info["id"]

    # -------------------------
    # Direct Stream URL
    # -------------------------
    async def _yt_dlp_get_stream(self, link: str, proxy: Optional[str] = None, prefer_video: bool = False) -> tuple[bool, Optional[str]]:
        args = ["yt-dlp", "--no-warnings", "--no-check-certificate", "--geo-bypass", "-g"]
        args += ["-f", "best[height<=?720][width<=?1280]"] if prefer_video else ["-f", "bestaudio/best"]
        args.append(link)
        env = os.environ.copy()
        if proxy:
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
            stdout, stderr = await proc.communicate()
            out_text, err_text = stdout.decode().strip(), stderr.decode().strip()
            if out_text:
                return True, out_text.splitlines()[0].strip()
            return False, err_text or "yt-dlp failed"
        except Exception as e:
            return False, repr(e)

    async def video(self, link: str, videoid: Union[bool, str] = None) -> tuple[int, str]:
        if videoid:
            link = self.BASE_URL + link
        if "&" in link:
            link = link.split("&")[0]
        ok, result = await self._yt_dlp_get_stream(link, proxy=None, prefer_video=True)
        if ok:
            return 1, result
        proxy = self._choose_proxy()
        if proxy:
            ok2, res2 = await self._yt_dlp_get_stream(link, proxy=proxy, prefer_video=True)
            if ok2:
                return 1, res2
        if SERVER_RELAY:
            relay_url = SERVER_RELAY + link
            try:
                r = await self._session.head(relay_url, timeout=10)
                if r.status_code in (200, 302, 303):
                    return 1, relay_url
            except:
                pass
        return 0, result or "Unable to get direct stream URL"

    async def playlist(self, link: str, limit: int, user_id: int, videoid: Union[bool, str] = None) -> list[str]:
        if videoid:
            link = self.PLAYLIST_BASE + link
        if "&" in link:
            link = link.split("&")[0]
        proc = await asyncio.create_subprocess_shell(
            f"yt-dlp -i --get-id --flat-playlist --playlist-end {limit} --skip-download {link}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return [key for key in out.decode("utf-8").split("\n") if key]
