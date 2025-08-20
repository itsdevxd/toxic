import asyncio
import re
import yt_dlp
from dataclasses import dataclass
from typing import Any, Optional, Union
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from youtubesearchpython.__future__ import VideosSearch
import os

# Optional Proxy (set env var YTDLP_PROXY="https://host:port" ya http://..)
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", None)

@dataclass
class DownloadResult:
    success: bool
    url: Optional[str] = None
    error: Optional[str] = None

class YouTubeAPI:
    BASE_URL = "https://www.youtube.com/watch?v="
    REGEX = r"(?:youtube\\.com|youtu\\.be)"

    def __init__(self, *_: Any, **__: Any):
        pass

    async def close(self) -> None:
        return None

    # ------------------- yt-dlp helpers ------------------- #
    @staticmethod
    def _ydl(proxy: Optional[str] = None) -> yt_dlp.YoutubeDL:
        opts = {
            "quiet": True,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "bestaudio/best",
        }
        if proxy:
            opts["proxy"] = proxy
        return yt_dlp.YoutubeDL(opts)

    @staticmethod
    def _best_audio_url(info: dict) -> Optional[str]:
        audio_formats = [
            f for f in info.get("formats", [])
            if f.get("acodec") and f.get("acodec") != "none"
            and (not f.get("vcodec") or f.get("vcodec") == "none")
            and f.get("url")
        ]
        if not audio_formats:
            return None
        audio_formats.sort(key=lambda f: (f.get("abr") or 0, f.get("asr") or 0, f.get("tbr") or 0))
        return audio_formats[-1]["url"]

    # ------------------- util ------------------- #
    async def exists(self, link: str, videoid: Union[bool, str] = None) -> bool:
        if videoid:
            link = self.BASE_URL + str(link)
        return bool(re.search(self.REGEX, link))

    async def url(self, message_1: Message) -> Optional[str]:
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)
        text, offset, length = "", None, None
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

    # ------------------- search ------------------- #
    async def search(self, query: str) -> Optional[dict]:
        try:
            vs = VideosSearch(query, limit=1)
            data = await vs.next()
            results = data.get("result", [])
            if not results:
                return None
            top = results[0]
            return {
                "title": top.get("title"),
                "id": top.get("id"),
                "link": top.get("link"),
                "duration": top.get("duration"),
                "thumbnail": (top.get("thumbnails") or [{}])[0].get("url", ""),
            }
        except Exception:
            return None

    # ------------------- audio extraction ------------------- #
    async def direct_audio(self, link: str) -> Optional[str]:
        try:
            with self._ydl(YTDLP_PROXY) as ydl:
                info = ydl.extract_info(link, download=False)
                a_url = self._best_audio_url(info)
                if a_url:
                    return a_url
            # fallback to yt-dlp -g bestaudio
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "-g", "-f", "bestaudio/best", link,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            a_url = stdout.decode().split("\n")[0].strip()
            return a_url or None
        except Exception:
            return None

    # ------------------- main call (used by bot) ------------------- #
    async def download(self, query: str, *_: Any, **__: Any) -> Union[str, tuple[str, bool]]:
        """
        User input ho sakta hai song ka naam ya YouTube link.
        Agar naam hai → YouTube search karega, top result lega.
        Fir uska direct audio URL nikal ke return karega.
        """
        if "youtube.com/watch" in query or "youtu.be/" in query:
            link = query
        else:
            result = await self.search(query)
            if not result:
                return "", None
            link = result["link"]

        audio_url = await self.direct_audio(link)
        if not audio_url:
            return "", None
        return audio_url, None
