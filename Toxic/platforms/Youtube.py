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
from Toxic.utils.database import is_on_off
from Toxic.utils.formatters import time_to_seconds
from config import API_URL, API_KEY, DOWNLOADS_DIR

# ---------- Configuration via env (set these in your environment or config) ----------
# Optional comma-separated HTTPS proxies, like: "https://user:pass@proxy1:port,https://proxy2:port"
PROXY_LIST_ENV = os.getenv("YTDL_PROXIES", "")
# Optional single HTTPS relay server that accepts a 'url' param and proxies content
# Example: "https://my-relay.example.com/proxy?url="
SERVER_RELAY = os.getenv("https://toxicapi.vercel.app/api/proxy?url=", "")  # must end with '=' or be format that allows appending ?url=
# Optional: maximum retries on network errors
MAX_NETWORK_RETRIES = int(os.getenv("YTDL_MAX_RETRIES", "3"))
# Timeout seconds for httpx requests
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
        # httpx AsyncClient will be used for non-yt-dlp network calls
        self._session = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self._timeout,
                read=self._timeout,
                write=self._timeout,
                pool=self._timeout
            ),
            follow_redirects=max_redirects > 0,
            max_redirects=max_redirects,
        )
        self._regex = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        # prepare proxies list from ENV
        self._proxies = self._parse_proxies(PROXY_LIST_ENV)

    async def close(self) -> None:
        try:
            await self._session.aclose()
        except Exception as e:
            LOGGER(__name__).error("Error closing HTTP session: %s", repr(e))

    @staticmethod
    def get_cookie_file() -> Optional[str]:
        """
        Kept for compatibility — but default strategy is to avoid cookies.
        If user wants cookies, put .txt cookie files in 'cookies' folder.
        """
        cookie_dir = "cookies"
        try:
            if not os.path.exists(cookie_dir):
                return None
            files = os.listdir(cookie_dir)
            cookies_files = [f for f in files if f.endswith(".txt")]
            if not cookies_files:
                return None
            return os.path.join(cookie_dir, random.choice(cookies_files))
        except Exception:
            return None

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
        # minimal user-agent to avoid being completely blocked
        headers.setdefault("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36")
        return headers

    async def download_file(self, url: str, file_path: Optional[Union[str, Path]] = None, overwrite: bool = False, use_proxy: Optional[str] = None, **kwargs: Any) -> DownloadResult:
        """
        Downloads a file via httpx streaming. If use_proxy provided, route via that proxy URL.
        """
        if not url:
            return DownloadResult(success=False, error="Empty URL provided")
        headers = self._get_headers(url, kwargs.pop("headers", {}))
        proxies = None
        if use_proxy:
            # httpx expects mapping for proxies
            proxies = {
                "all://": use_proxy
            }
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
                        LOGGER(__name__).debug("Successfully downloaded file to %s", path)
                        return DownloadResult(success=True, file_path=path)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                LOGGER(__name__).warning("HTTP status %s while downloading %s (attempt %s)", status, url, attempt + 1)
                # if rate-limited or forbidden, try fallback proxy / relay
                last_err = f"HTTP {status}: {e.response.text[:200]}"
            except Exception as e:
                last_err = repr(e)
                LOGGER(__name__).warning("Download attempt failed: %s (attempt %s)", repr(e), attempt + 1)
            await asyncio.sleep(1 + attempt * 2)
        return DownloadResult(success=False, error=f"Failed to download {url}: {last_err}")

    async def make_request(self, url: str, max_retries: int = MAX_RETRIES, backoff_factor: float = BACKOFF_FACTOR, use_proxy: Optional[str] = None, **kwargs: Any) -> Optional[dict[str, Any]]:
        """
        JSON request helper with optional proxy.
        """
        if not url:
            LOGGER(__name__).warning("Empty URL provided")
            return None
        headers = self._get_headers(url, kwargs.pop("headers", {}))
        proxies = None
        if use_proxy:
            proxies = {"all://": use_proxy}
        for attempt in range(max_retries):
            try:
                start = time.monotonic()
                async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT, proxies=proxies) as client:
                    response = await client.get(url, headers=headers, **kwargs)
                    response.raise_for_status()
                    duration = time.monotonic() - start
                    LOGGER(__name__).debug("Request to %s succeeded in %.2fs", url, duration)
                    # return parsed json if possible, else raw
                    try:
                        return response.json()
                    except ValueError:
                        return {"text": response.text}
            except httpx.HTTPStatusError as e:
                try:
                    error_text = e.response.text
                except Exception:
                    error_text = str(e)
                LOGGER(__name__).warning("HTTP error %s for %s: %s", e.response.status_code, url, error_text[:200])
                if attempt == max_retries - 1:
                    return None
            except httpx.RequestError as e:
                LOGGER(__name__).warning("Request failed for %s: %s", url, repr(e))
                if attempt == max_retries - 1:
                    return None
            await asyncio.sleep(backoff_factor * (2 ** attempt))
        LOGGER(__name__).error("All retries failed for URL: %s", url)
        return None

    @staticmethod
    def _handle_http_error(e: Exception, url: str) -> str:
        try:
            import httpx as _httpx
            if isinstance(e, _httpx.TooManyRedirects):
                return f"Too many redirects for {url}: {repr(e)}"
            elif isinstance(e, _httpx.HTTPStatusError):
                try:
                    return f"HTTP error {e.response.status_code} for {url}: {e.response.text}"
                except Exception:
                    return f"HTTP error {e.response.status_code} for {url}."
            elif isinstance(e, _httpx.ReadTimeout):
                return f"Read timeout for {url}: {repr(e)}"
            elif isinstance(e, _httpx.RequestError):
                return f"Request failed for {url}: {repr(e)}"
        except Exception:
            pass
        return f"Unexpected error for {url}: {repr(e)}"

    async def download_with_api(self, video_id: str, is_video: bool = False) -> Optional[Path]:
        """
        Existing API pathway (if configured). Left unchanged except defensive checks.
        """
        if not API_URL or not API_KEY:
            LOGGER(__name__).warning("API_URL or API_KEY is not set")
            return None
        if not video_id:
            LOGGER(__name__).warning("Video ID is None")
            return None
        public_url = await self.make_request(f"{API_URL}/yt?id={video_id}&video={is_video}")
        if not public_url:
            LOGGER(__name__).error("No response from API")
            return None
        dl_url = public_url.get("results")
        if not dl_url:
            LOGGER(__name__).error("API response is empty")
            return None
        # If API gives t.me link, download from Telegram
        if not re.match(r"https:\/\/t\.me\/(?:[a-zA-Z0-9_]{5,}|c\/\d+)\/(\d+)", dl_url):
            dl = await self.download_file(dl_url)
            return dl.file_path if dl.success else None
        try:
            match = re.match(r"https:\/\/t\.me\/([a-zA-Z0-9_]{5,}|c\/\d+)\/(\d+)", dl_url)
            if not match:
                LOGGER(__name__).error("Invalid Telegram URL format")
                return None
            chat_id, message_id = match.groups()
            if chat_id.startswith("c/"):
                chat_id = f"-100{chat_id[2:]}"
            msg = await app.get_messages(chat_id=chat_id, message_ids=int(message_id))
            if not msg:
                LOGGER(__name__).error("Message not found in Telegram chat")
                return None
            path = await msg.download()
            return Path(path) if path else None
        except errors.FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return await self.download_with_api(video_id, is_video)
        except errors.ChatForbidden:
            LOGGER(__name__).error(f"Bot does not have access to the Telegram chat: {chat_id}")
            return None
        except errors.ChannelInvalid:
            LOGGER(__name__).error(f"Invalid Telegram channel or group: {chat_id}")
            return None
        except Exception as e:
            LOGGER(__name__).error(f"Error getting message from Telegram chat: {e}")
            return None

    async def exists(self, link: str, videoid: Union[bool, str] = None) -> bool:
        if videoid:
            link = self.BASE_URL + link
        return bool(re.search(self.REGEX, link))

    async def url(self, message_1: Message) -> Optional[str]:
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)
        text = ""
        offset = None
        length = None
        for message in messages:
            if offset:
                break
            if message.entities:
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = message.text or message.caption
                        offset, length = entity.offset, entity.length
                        break
            elif message.caption_entities:
                for entity in message.caption_entities:
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
        return text[offset:offset + length] if offset is not None else None

    async def details(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.BASE_URL + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        for result in (await results.next())["result"]:
            title = result["title"]
            duration_min = result["duration"]
            thumbnail = result["thumbnails"][0]["url"].split("?")[0]
            vidid = result["id"]
            duration_sec = 0 if str(duration_min) == "None" else int(time_to_seconds(duration_min))
        return title, duration_min, duration_sec, thumbnail, vidid

    async def title(self, link: str, videoid: Union[bool, str] = None) -> str:
        if videoid:
            link = self.BASE_URL + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        return (await results.next())["result"][0]["title"]

    async def duration(self, link: str, videoid: Union[bool, str] = None) -> str:
        if videoid:
            link = self.BASE_URL + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        return (await results.next())["result"][0]["duration"]

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None) -> str:
        if videoid:
            link = self.BASE_URL + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        return (await results.next())["result"][0]["thumbnails"][0]["url"].split("?")[0]

    # -------------------------
    # Core: get direct stream URL (safe, without cookies if possible)
    # Strategy:
    # 1) try yt-dlp -g (no cookies)
    # 2) if fails or returns empty/403/429 -> try yt-dlp with a proxy from env (sets HTTP(S)_PROXY for subprocess)
    # 3) if still fails and SERVER_RELAY specified -> return relay-proxied URL (relay must be configured by user)
    # -------------------------
    async def _yt_dlp_get_stream(self, link: str, proxy: Optional[str] = None, prefer_video: bool = False) -> tuple[bool, Optional[str]]:
        """
        Returns (success(bool), direct_url_or_error(str))
        """
        # build base args
        # -g => print direct URL (best) ; avoid cookiefile by default
        args = [
            "yt-dlp",
            "--no-warnings",
            "--no-check-certificate",
            "--geo-bypass",
            "-g",
        ]
        # prefer video or audio? pass format accordingly
        if prefer_video:
            args += ["-f", "best[height<=?720][width<=?1280]"]
        else:
            args += ["-f", "bestaudio/best"]
        # link
        args.append(link)

        env = os.environ.copy()
        if proxy:
            # yt-dlp uses environment variables for proxy: HTTP_PROXY, HTTPS_PROXY
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await proc.communicate()
            out_text = stdout.decode().strip()
            err_text = stderr.decode().strip()
            if out_text:
                # return the first line (direct URL)
                first = out_text.splitlines()[0].strip()
                return True, first
            else:
                LOGGER(__name__).warning("yt-dlp -g returned empty stdout. stderr: %s", err_text[:300])
                return False, err_text or "yt-dlp failed without stdout"
        except Exception as e:
            LOGGER(__name__).error("Error running yt-dlp -g: %s", repr(e))
            return False, repr(e)

    async def video(self, link: str, videoid: Union[bool, str] = None) -> tuple[int, str]:
        """
        Return (1, url) on success. Keep compatibility with older code.
        """
        # API-first (if present)
        if dl := await self.download_with_api(link, True):
            return 1, str(dl)

        if videoid:
            link = self.BASE_URL + link
        if "&" in link:
            link = link.split("&")[0]

        # 1) Try direct call without cookies/proxy
        ok, result = await self._yt_dlp_get_stream(link, proxy=None, prefer_video=True)
        if ok:
            return 1, result

        # 2) Try with configured proxy (choose random)
        proxy = self._choose_proxy()
        if proxy:
            ok2, res2 = await self._yt_dlp_get_stream(link, proxy=proxy, prefer_video=True)
            if ok2:
                return 1, res2
            else:
                LOGGER(__name__).warning("yt-dlp via proxy %s failed: %s", proxy, res2)

        # 3) Try relay server (if configured) - relay must accept url param and stream/redirect
        if SERVER_RELAY:
            # some relays accept trailing url param, some accept ?url= ; code expects SERVER_RELAY to be prefix like https://relay/?url=
            relay_url = SERVER_RELAY + link
            # Validate relay URL with a quick HEAD (or GET w/ no body)
            try:
                r = await self._session.head(relay_url, timeout=10)
                if r.status_code in (200, 302, 303):
                    return 1, relay_url
                # fallback to GET if HEAD not allowed
                r2 = await self._session.get(relay_url, timeout=10)
                if r2.status_code in (200, 302, 303):
                    return 1, relay_url
            except Exception as e:
                LOGGER(__name__).warning("Relay check failed: %s", repr(e))

        # 4) final fallback: return error message (caller should handle)
        return 0, result or "Unable to get direct stream URL"

    async def playlist(self, link: str, limit: int, user_id: int, videoid: Union[bool, str] = None) -> list[str]:
        if videoid:
            link = self.PLAYLIST_BASE + link
        if "&" in link:
            link = link.split("&")[0]
        proc = await asyncio.create_subprocess_shell(
            f"yt-dlp -i --get-id --flat-playlist --playlist-end {limit} --skip-download {link}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, errorz = await proc.communicate()
        result = out.decode("utf-8").split("\n") if not errorz or "unavailable videos are hidden" in errorz.decode("utf-8").lower() else []
        return [key for key in result if key]

    async def track(self, link: str, videoid: Union[bool, str] = None) -> tuple[dict, str]:
        if videoid:
            link = self.BASE_URL + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        data = (await results.next())["result"][0]
        return data, data["id"]
