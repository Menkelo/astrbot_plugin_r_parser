import asyncio
import socket
from abc import ABC
from asyncio import Task
from collections.abc import Callable, Coroutine
from pathlib import Path
from re import Match, Pattern, compile
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, List, Dict

import aiohttp
from curl_cffi.requests import AsyncSession
from typing_extensions import Unpack
from astrbot.api import logger

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..constants import ANDROID_HEADER, COMMON_HEADER, IOS_HEADER
from ..data import (
    AudioContent,
    Author,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    ParseResultKwargs,
    Platform,
    VideoContent,
)
from ..download import Downloader
from ..exception import ParseException

T = TypeVar("T", bound="BaseParser")
HandlerFunc = Callable[[T, Match[str]], Coroutine[Any, Any, ParseResult]]
KeyPatterns = list[tuple[str, Pattern[str]]]

_PARSER_META = "_parser_meta"


def handle(keyword: str, pattern: str):
    """娉ㄥ唽澶勭悊鍣ㄨ�楗板櫒"""
    def decorator(func: HandlerFunc[T]) -> HandlerFunc[T]:
        if not hasattr(func, _PARSER_META):
            setattr(func, _PARSER_META, [])
        meta = getattr(func, _PARSER_META)
        meta.append((keyword, compile(pattern)))
        return func
    return decorator


class BaseParser:
    """鎵€鏈夊钩鍙� Parser 鐨勬娊璞″熀绫�"""

    _registry: ClassVar[list[type["BaseParser"]]] = []
    platform: ClassVar[Platform]

    _dispatch_map: ClassVar[dict[str, str]]
    _key_patterns: ClassVar[KeyPatterns]

    def __init__(
        self,
        config: AstrBotConfig,
        downloader: Downloader,
    ):
        self.headers = COMMON_HEADER.copy()
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = ANDROID_HEADER.copy()
        self.config = config
        self.downloader = downloader

        self._session: AsyncSession | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__:
            BaseParser._registry.append(cls)

        cls._dispatch_map = {}
        cls._key_patterns = []

        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and hasattr(attr, _PARSER_META):
                meta = getattr(attr, _PARSER_META)
                for keyword, pattern in meta:
                    cls._dispatch_map[keyword] = attr_name
                    cls._key_patterns.append((keyword, pattern))

        cls._key_patterns.sort(key=lambda x: -len(x[0]))

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseParser"]]:
        return cls._registry

    @property
    def client(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                impersonate="chrome120",
                timeout=15,
                verify=False,
            )
        return self._session

    async def close_session(self) -> None:
        if self._session:
            self._session.close()
            self._session = None

    async def parse(self, keyword: str, searched: Match[str]) -> ParseResult:
        """瑙ｆ瀽 URL 鎻愬彇淇℃伅"""
        method_name = self._dispatch_map.get(keyword)
        if not method_name:
            raise ParseException(f"鏈�壘鍒板叧閿�瘝 {keyword} 瀵瑰簲鐨勫�鐞嗘柟娉�")

        handler = getattr(self, method_name)
        return await handler(searched)

    async def parse_with_redirect(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> ParseResult:
        redirect_url = await self.get_redirect_url(url, headers=headers or self.headers)
        if redirect_url == url:
            raise ParseException(f"鏃犳硶閲嶅畾鍚� URL: {url}")
        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    @classmethod
    def search_url(cls, url: str) -> tuple[str, Match[str]]:
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if searched := pattern.search(url):
                return keyword, searched
        raise ParseException(f"鏃犳硶鍖归厤 {url}")

    @classmethod
    def result(cls, **kwargs: Unpack[ParseResultKwargs]) -> ParseResult:
        return ParseResult(platform=cls.platform, **kwargs)

    async def http_get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        allow_redirects: bool = True,
        timeout: int = 15,
    ):
        """
        缁熶竴 GET 璇锋眰鍏ュ彛:
        1) 浼樺厛 curl_cffi锛堝甫閲嶈瘯锛�
        2) 澶辫触鍚庨檷绾� aiohttp锛堝己鍒禝Pv4 + 閲嶈瘯锛�
        杩斿洖瀵硅薄鍏煎�瀛楁�: status_code / headers / url / text / content
        """
        headers = headers or self.headers
        retries = 3

        # 绗�竴灞傦細curl_cffi 閲嶈瘯
        last_curl_err: Exception | None = None
        for i in range(retries):
            try:
                return await self.client.get(
                    url,
                    headers=headers,
                    params=params,
                    allow_redirects=allow_redirects,
                    timeout=timeout,
                    verify=False,
                )
            except Exception as e:
                last_curl_err = e
                if i < retries - 1:
                    await asyncio.sleep(0.25 * (i + 1))

        logger.warning(f"curl_cffi GET澶辫触锛屽皾璇昦iohttp鍏滃簳: {last_curl_err}")

        # 绗�簩灞傦細aiohttp 寮哄埗 IPv4 + 閲嶈瘯
        last_aio_err: Exception | None = None
        for i in range(retries):
            conn = aiohttp.TCPConnector(
                ssl=False,
                family=socket.AF_INET,   # 寮哄埗 IPv4
                ttl_dns_cache=300,
                use_dns_cache=True,
            )
            timeout_conf = aiohttp.ClientTimeout(
                total=timeout,
                connect=min(10, timeout),
                sock_connect=min(10, timeout),
                sock_read=timeout,
            )

            try:
                async with aiohttp.ClientSession(
                    connector=conn,
                    timeout=timeout_conf,
                    headers=headers,
                ) as session:
                    async with session.get(
                        url,
                        params=params,
                        allow_redirects=allow_redirects,
                    ) as resp:
                        body = await resp.read()

                        class _Resp:
                            def __init__(self, _resp, _body: bytes):
                                self.status_code = _resp.status
                                self.headers = dict(_resp.headers)
                                self.url = str(_resp.url)
                                self.content = _body
                                self.text = _body.decode(errors="ignore")

                        return _Resp(resp, body)

            except Exception as e:
                last_aio_err = e
                if i < retries - 1:
                    await asyncio.sleep(0.35 * (i + 1))
            finally:
                await conn.close()

        raise ParseException(f"HTTP GET澶辫触(curl+aiohttp): {last_aio_err or last_curl_err}")

    async def get_redirect_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        headers = headers or COMMON_HEADER.copy()
        try:
            resp = await self.http_get(
                url,
                headers=headers,
                allow_redirects=False,
                timeout=15,
            )
            if resp.status_code >= 400:
                raise ParseException(f"redirect check {resp.status_code}")
            return resp.headers.get("Location", url)
        except Exception as e:
            raise ParseException(f"閲嶅畾鍚戞�娴嬪け璐�: {e}")

    async def get_final_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        headers = headers or COMMON_HEADER.copy()
        try:
            resp = await self.http_get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=15,
            )
            return str(resp.url)
        except Exception:
            return url

    async def get_search_data(self, keyword: str) -> List[Dict[str, Any]]:
        """
        鎼滅储鎺ュ彛锛岃繑鍥炴爣鍑嗗寲鐨勭粨鏋滃垪琛�
        """
        return []

    def create_author(
        self,
        name: str,
        avatar_url: str | None = None,
        description: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ):
        return Author(name=name, avatar=None, description=description)

    def create_video_content(
        self,
        url_or_task: str | Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        ext_headers: dict[str, str] | None = None,
    ):
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_video(
                url_or_task, ext_headers=ext_headers or self.headers
            )
        return VideoContent(url_or_task, None, duration)

    def create_image_contents(
        self,
        image_urls: list[str],
        ext_headers: dict[str, str] | None = None,
    ):
        contents: list[ImageContent] = []
        for url in image_urls:
            task = self.downloader.download_img(url, ext_headers=ext_headers or self.headers)
            contents.append(ImageContent(task))
        return contents

    def create_dynamic_contents(
        self,
        dynamic_urls: list[str],
        ext_headers: dict[str, str] | None = None,
    ):
        contents: list[DynamicContent] = []
        for url in dynamic_urls:
            task = self.downloader.download_video(url, ext_headers=ext_headers or self.headers)
            contents.append(DynamicContent(task))
        return contents

    def create_audio_content(
        self,
        url_or_task: str | Task[Path],
        duration: float = 0.0,
    ):
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_audio(
                url_or_task, ext_headers=self.headers
            )
        return AudioContent(url_or_task, duration)

    def create_graphics_content(
        self,
        image_url: str,
        text: str | None = None,
        alt: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ):
        image_task = self.downloader.download_img(image_url, ext_headers=ext_headers or self.headers)
        return GraphicsContent(image_task, text, alt)

    def create_file_content(
        self,
        url_or_task: str | Task[Path],
        name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ):
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_file(
                url_or_task, ext_headers=ext_headers or self.headers, file_name=name
            )
        return FileContent(url_or_task)
