from abc import ABC
from asyncio import Task
from collections.abc import Callable, Coroutine
from pathlib import Path
from re import Match, Pattern, compile
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, List, Dict

# === 新增：引入 aiohttp 用于兜底 ===
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
    """注册处理器装饰器"""
    def decorator(func: HandlerFunc[T]) -> HandlerFunc[T]:
        if not hasattr(func, _PARSER_META):
            setattr(func, _PARSER_META, [])
        meta = getattr(func, _PARSER_META)
        meta.append((keyword, compile(pattern)))
        return func
    return decorator


class BaseParser:
    """所有平台 Parser 的抽象基类"""

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
        """解析 URL 提取信息"""
        method_name = self._dispatch_map.get(keyword)
        if not method_name:
            raise ParseException(f"未找到关键词 {keyword} 对应的处理方法")

        handler = getattr(self, method_name)
        return await handler(searched)

    async def parse_with_redirect(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> ParseResult:
        redirect_url = await self.get_redirect_url(url, headers=headers or self.headers)
        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")
        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    @classmethod
    def search_url(cls, url: str) -> tuple[str, Match[str]]:
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if searched := pattern.search(url):
                return keyword, searched
        raise ParseException(f"无法匹配 {url}")

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
        统一 GET 请求入口:
        优先 curl_cffi；失败自动降级 aiohttp。
        返回对象兼容常用字段: status_code / headers / url / text / content
        """
        headers = headers or self.headers
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
            logger.warning(f"curl_cffi GET失败，尝试aiohttp兜底: {e}")
            conn = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                connector=conn,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers=headers,
            ) as session:
                async with session.get(
                    url,
                    params=params,
                    allow_redirects=allow_redirects,
                ) as resp:
                    body = await resp.read()

                    class _Resp:
                        def __init__(self):
                            self.status_code = resp.status
                            self.headers = dict(resp.headers)
                            self.url = str(resp.url)
                            self.content = body
                            self.text = body.decode(errors="ignore")

                    return _Resp()

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
            raise ParseException(f"重定向检测失败: {e}")

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
        搜索接口，返回标准化的结果列表
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
