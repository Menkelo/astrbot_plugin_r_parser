import asyncio
import socket
from asyncio import Task, create_task
from collections.abc import Callable, Coroutine
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, ParamSpec, TypeVar

import aiofiles
import aiohttp
import yt_dlp
from curl_cffi import CurlHttpVersion
from curl_cffi.requests import AsyncSession, RequestsError
from msgspec import Struct, convert
from tqdm.asyncio import tqdm

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from .constants import COMMON_HEADER, IOS_HEADER
from .exception import (
    DownloadException,
    ParseException,
    SizeLimitException,
)
from .utils import LimitedSizeDict, generate_file_name, merge_av, safe_unlink

P = ParamSpec("P")
T = TypeVar("T")


def auto_task(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Task[T]]:
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Task[T]:
        coro = func(*args, **kwargs)
        name = " | ".join(str(arg) for arg in args if isinstance(arg, str))
        return create_task(coro, name=func.__name__ + " | " + name)
    return wrapper


class VideoInfo(Struct):
    title: str | None = None
    channel: str | None = None
    uploader: str | None = None
    duration: float | None = None
    timestamp: int | None = None
    thumbnail: str | None = None
    description: str | None = None
    channel_id: str | None = None

    @property
    def author_name(self) -> str:
        c = self.channel or "Unknown"
        u = self.uploader or ""
        return f"{c}@{u}" if u else c


class Downloader:
    """
    全能下载器
    策略优先级: curl_cffi -> aiohttp -> yt-dlp
    """

    def __init__(self, config: AstrBotConfig):
        self.config = config
        self.cache_dir = Path(config["cache_dir"])

        perf_conf = self.config.get("performance", {})
        self.default_max_size = perf_conf.get("source_max_size", 90)

        self.headers: dict[str, str] = COMMON_HEADER.copy()
        self.info_cache: LimitedSizeDict[str, VideoInfo] = LimitedSizeDict()

        concurrency = perf_conf.get("max_concurrent_downloads", 5)
        self.sem = asyncio.Semaphore(concurrency)
        logger.info(f"下载器已初始化，最大并发数: {concurrency}")

        self.douyin_strategy_idx = 0

        # 关键修复：强制 HTTP/1.1，规避部分 TLS/ALPN 问题
        self.session = AsyncSession(
            impersonate="chrome120",
            timeout=300,
            headers=self.headers,
            verify=False,
            http_version=CurlHttpVersion.V1_1,
        )

    @auto_task
    async def streamd(
        self,
        url: str,
        *,
        file_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        max_size_mb: int | None = None,
    ) -> Path:
        if not file_name:
            file_name = generate_file_name(url)
        file_path = self.cache_dir / file_name

        if file_path.exists():
            if file_path.stat().st_size < 100:
                await safe_unlink(file_path)
            else:
                return file_path

        limit = max_size_mb if max_size_mb is not None else self.default_max_size

        async with self.sem:
            if "douyinpic.com" in url:
                return await self._download_douyin_image(url, file_path, file_name, limit)

            headers = self.headers.copy()
            if ext_headers:
                headers.update(ext_headers)

            return await self._download_generic(url, file_path, file_name, headers, limit)

    async def _download_douyin_image(self, url: str, file_path: Path, file_name: str, limit: int) -> Path:
        try:
            u = urlparse(url)
            self_referer = f"{u.scheme}://{u.netloc}/"
        except Exception:
            self_referer = "https://www.douyin.com/"

        strategies = [
            {
                "name": "PC-Clean",
                "impersonate": "chrome120",
                "headers": {"User-Agent": COMMON_HEADER["User-Agent"]},
            },
            {
                "name": "PC-SelfReferer",
                "impersonate": "chrome120",
                "headers": {
                    "User-Agent": COMMON_HEADER["User-Agent"],
                    "Referer": self_referer,
                    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                },
            },
            {
                "name": "Mobile-Safari",
                "impersonate": "safari15_3",
                "headers": {
                    "User-Agent": IOS_HEADER["User-Agent"],
                    "Accept": "*/*",
                },
            },
        ]

        start_idx = self.douyin_strategy_idx
        strategy_count = len(strategies)
        last_error = None

        for i in range(strategy_count):
            current_idx = (start_idx + i) % strategy_count
            strategy = strategies[current_idx]

            try:
                async with AsyncSession(
                    impersonate=strategy["impersonate"],
                    timeout=30,
                    verify=False,
                    headers=strategy["headers"],
                    http_version=CurlHttpVersion.V1_1,
                ) as temp_session:
                    response = await temp_session.get(url, stream=True)
                    if response.status_code >= 400:
                        if response.status_code in [502, 503, 504]:
                            await asyncio.sleep(1)
                        raise RequestsError(f"HTTP {response.status_code}", response=response)

                    await self._save_response_to_file(response, file_path, file_name, limit)

                    if self.douyin_strategy_idx != current_idx:
                        self.douyin_strategy_idx = current_idx

                    return file_path
            except Exception as e:
                last_error = e
                await safe_unlink(file_path)
                if i == strategy_count - 1:
                    logger.warning(f"[抖音图片] curl 策略全部失败: {e}，尝试 aiohttp 兜底")
                continue

        try:
            fallback_headers = {
                "User-Agent": COMMON_HEADER["User-Agent"],
                "Referer": self_referer,
            }
            return await self._download_with_aiohttp(url, file_path, file_name, fallback_headers, limit)
        except Exception as e:
            logger.error(f"[抖音图片] aiohttp 兜底失败: {e}")
            raise DownloadException("抖音图片下载失败") from last_error

    async def _download_generic(self, url: str, file_path: Path, file_name: str, headers: dict, limit: int) -> Path:
        max_retries = 3

        for attempt in range(max_retries):
            current_headers = headers.copy()
            try:
                response = await self.session.get(url, headers=current_headers, stream=True)

                if response.status_code >= 400:
                    if response.status_code == 403 and "Referer" in current_headers:
                        current_headers.pop("Referer", None)
                        response = await self.session.get(url, headers=current_headers, stream=True)

                    if response.status_code >= 400:
                        raise RequestsError(f"HTTP {response.status_code}", response=response)

                content_type = response.headers.get("Content-Type", "")
                if "text/html" in content_type or "application/json" in content_type:
                    if not file_name.endswith((".html", ".json", ".txt")):
                        is_strict_referer = any(d in url for d in ["bilivideo.com", "douyin.com"])
                        if "Referer" in current_headers and not is_strict_referer:
                            current_headers.pop("Referer", None)
                            response = await self.session.get(url, headers=current_headers, stream=True)
                            content_type = response.headers.get("Content-Type", "")
                        if "text/html" in content_type or "application/json" in content_type:
                            raise DownloadException(f"服务端返回了非媒体类型: {content_type}")

                await self._save_response_to_file(response, file_path, file_name, limit)

                if file_path.exists() and file_path.stat().st_size < 100:
                    await safe_unlink(file_path)
                    raise DownloadException("下载文件过小，可能是无效文件")

                return file_path

            except SizeLimitException:
                await safe_unlink(file_path)
                raise

            except Exception as e:
                await safe_unlink(file_path)
                err_msg = str(e)

                # curl/TLS/连接错误时走 aiohttp
                should_fallback = any(
                    k in err_msg for k in [
                        "curl", "TLS", "HTTP/2", "Closed",
                        "HTTP 403", "Failed to perform", "SSL", "invalid library",
                    ]
                )

                if should_fallback:
                    logger.warning(f"curl_cffi 异常 ({err_msg})，尝试 aiohttp 兜底: {url}")
                    try:
                        return await self._download_with_aiohttp(url, file_path, file_name, current_headers, limit)
                    except Exception as aio_e:
                        logger.error(f"aiohttp 兜底失败: {aio_e}")
                        if attempt == max_retries - 1:
                            logger.warning("aiohttp 失败，尝试最终方案 yt-dlp 下载...")
                            try:
                                return await self._download_with_ytdlp_fallback(url, file_path, current_headers)
                            except Exception as yt_e:
                                logger.error(f"yt-dlp 兜底失败: {yt_e}")

                if attempt == max_retries - 1:
                    logger.error(f"下载最终失败: {url} | Error: {e}")
                    raise DownloadException("媒体下载失败") from e

                await asyncio.sleep(0.6 * (attempt + 1))

        raise DownloadException("媒体下载失败")

    async def _download_with_aiohttp(self, url: str, file_path: Path, file_name: str, headers: dict, limit: int) -> Path:
        # 关键修复：强制 IPv4，减小部分环境 DNS/IPv6 连接失败概率
        conn = aiohttp.TCPConnector(
            ssl=False,
            family=socket.AF_INET,
            ttl_dns_cache=300,
        )
        timeout = aiohttp.ClientTimeout(total=120, connect=20, sock_connect=20, sock_read=120)

        async with aiohttp.ClientSession(headers=headers, connector=conn, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise DownloadException(f"aiohttp HTTP {resp.status}")

                content_length = int(resp.headers.get("Content-Length", 0))
                if content_length and (content_length / 1024 / 1024) > limit:
                    raise SizeLimitException(f"媒体大小({content_length/1024/1024:.1f}MB)超过限制")

                chunk_size = 256 * 1024
                with self.get_progress_bar(file_name, content_length) as bar:
                    async with aiofiles.open(file_path, "wb") as f:
                        downloaded = 0
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            downloaded += len(chunk)
                            if limit and (downloaded / 1024 / 1024) > limit:
                                raise SizeLimitException("下载大小超限")
                            await f.write(chunk)
                            bar.update(len(chunk))

        if not file_path.exists() or file_path.stat().st_size < 100:
            await safe_unlink(file_path)
            raise DownloadException("aiohttp 下载文件过小")

        logger.info(f"aiohttp 兜底下载成功: {file_name}")
        return file_path

    async def _download_with_ytdlp_fallback(self, url: str, file_path: Path, headers: dict) -> Path:
        await safe_unlink(file_path)

        opts = {
            "quiet": True,
            "no_warnings": True,
            "http_headers": headers,
            "outtmpl": str(file_path),
            "skip_download": False,
            "hls_use_mpegts": True,
            "nocheckcertificate": True,
            "http_chunk_size": 10485760,
        }

        def run_ytdlp():
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        await asyncio.to_thread(run_ytdlp)

        if file_path.exists() and file_path.stat().st_size > 100:
            return file_path

        parent = file_path.parent
        stem = file_path.name
        possible_files = list(parent.glob(f"{stem}*"))
        if not possible_files:
            stem_no_ext = file_path.stem
            possible_files = list(parent.glob(f"{stem_no_ext}*"))

        for f in possible_files:
            if f.is_file() and f.stat().st_size > 100:
                logger.info(f"yt-dlp 修正文件名: {f.name} -> {file_path.name}")
                f.rename(file_path)
                return file_path

        raise DownloadException("yt-dlp 兜底下载未生成预期文件")

    async def _save_response_to_file(self, response, file_path, file_name, limit_mb):
        content_length = int(response.headers.get("Content-Length", 0))
        if content_length and (content_length / 1024 / 1024) > limit_mb:
            raise SizeLimitException(f"媒体大小({content_length/1024/1024:.1f}MB)超过限制({limit_mb}MB)")

        chunk_size = 256 * 1024

        with self.get_progress_bar(file_name, content_length) as bar:
            async with aiofiles.open(file_path, "wb") as file:
                downloaded = 0
                async for chunk in response.aiter_content(chunk_size=chunk_size):
                    downloaded += len(chunk)
                    if limit_mb and (downloaded / 1024 / 1024) > limit_mb:
                        raise SizeLimitException(f"媒体大小(>{limit_mb}MB)超过限制")
                    await file.write(chunk)
                    bar.update(len(chunk))

    @staticmethod
    def get_progress_bar(desc: str, total: int | None = None) -> tqdm:
        return tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            colour="green",
            desc=desc,
            mininterval=1.0,
            miniters=10,
        )

    @auto_task
    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        use_ytdlp: bool = False,
        cookiefile: Path | None = None,
        max_size_mb: int | None = None,
    ) -> Path:
        if use_ytdlp:
            return await self._ytdlp_download_video(url, cookiefile, video_name)

        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        return await self.streamd(url, file_name=video_name, ext_headers=ext_headers, max_size_mb=max_size_mb)

    @auto_task
    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        use_ytdlp: bool = False,
        cookiefile: Path | None = None,
        max_size_mb: int | None = None,
    ) -> Path:
        if use_ytdlp:
            return await self._ytdlp_download_audio(url, cookiefile, audio_name)

        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")
        return await self.streamd(url, file_name=audio_name, ext_headers=ext_headers, max_size_mb=max_size_mb)

    @auto_task
    async def download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        if file_name is None:
            file_name = generate_file_name(url, ".zip")
        return await self.streamd(url, file_name=file_name, ext_headers=ext_headers)

    @auto_task
    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        return await self.streamd(url, file_name=img_name, ext_headers=ext_headers)

    @auto_task
    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        ext_headers: dict[str, str] | None = None,
        max_size_mb: int | None = None,
    ) -> Path:
        try:
            v_path, a_path = await asyncio.gather(
                self.download_video(v_url, ext_headers=ext_headers, max_size_mb=max_size_mb),
                self.download_audio(a_url, ext_headers=ext_headers, max_size_mb=max_size_mb),
            )
            await merge_av(v_path=v_path, a_path=a_path, output_path=output_path)
            return output_path
        except SizeLimitException:
            raise
        except Exception as e:
            logger.error(f"合并下载失败: {e}")
            raise DownloadException(f"音视频下载合并失败: {e}")

    async def ytdlp_extract_info(self, url: str, cookiefile: Path | None = None) -> VideoInfo:
        if (info := self.info_cache.get(url)) is not None:
            return info

        opts = {
            "quiet": True,
            "skip_download": True,
            "force_generic_extractor": True,
            "cookiefile": None,
        }
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)

        with yt_dlp.YoutubeDL(opts) as ydl:
            raw = await asyncio.to_thread(ydl.extract_info, url, download=False)
            if not raw:
                raise ParseException("获取视频信息失败")

        info = convert(raw, VideoInfo)
        self.info_cache[url] = info
        return info

    async def _ytdlp_download_video(
        self, url: str, cookiefile: Path | None = None, video_name: str | None = None
    ) -> Path:
        _ = await self.ytdlp_extract_info(url, cookiefile)

        if video_name:
            file_stem = Path(video_name).stem
        else:
            file_stem = generate_file_name(url)

        video_path = self.cache_dir / f"{file_stem}.mp4"
        if video_path.exists() and video_path.stat().st_size > 100:
            return video_path

        opts = {
            "outtmpl": str(self.cache_dir / file_stem) + ".%(ext)s",
            "merge_output_format": "mp4",
            "format": "best[height<=720]/bestvideo[height<=720]+bestaudio/best",
            "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
            "cookiefile": None,
            "nocheckcertificate": True,
        }
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)

        async with self.sem:
            with yt_dlp.YoutubeDL(opts) as ydl:
                await asyncio.to_thread(ydl.download, [url])

        return video_path

    async def _ytdlp_download_audio(
        self, url: str, cookiefile: Path | None, audio_name: str | None = None
    ) -> Path:
        if audio_name:
            file_stem = Path(audio_name).stem
        else:
            file_stem = generate_file_name(url)

        audio_path = self.cache_dir / f"{file_stem}.m4a"
        if audio_path.exists() and audio_path.stat().st_size > 100:
            return audio_path

        opts = {
            "outtmpl": str(self.cache_dir / file_stem) + ".%(ext)s",
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
            "cookiefile": None,
            "nocheckcertificate": True,
        }
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)

        async with self.sem:
            with yt_dlp.YoutubeDL(opts) as ydl:
                await asyncio.to_thread(ydl.download, [url])

        return audio_path

    async def close(self):
        self.session.close()
