import json
import re
from typing import Any, ClassVar

from msgspec import Struct, convert, field

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..download import Downloader
from .base import BaseParser, ParseException, Platform, handle


class XiaoHongShuParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="xiaohongshu", display_name="小红书")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        explore_headers = {
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
            )
        }
        self.headers.update(explore_headers)
        discovery_headers = {
            "origin": "https://www.xiaohongshu.com",
            "x-requested-with": "XMLHttpRequest",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        self.ios_headers.update(discovery_headers)

    @handle("xhslink.com", r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]*")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url, self.ios_headers)

    @handle(
        "hongshu.com/explore",
        r"explore/(?P<xhs_id>[0-9a-zA-Z]+)\?[A-Za-z0-9._%&+=/#@-]*",
    )
    async def _parse_explore(self, searched: re.Match[str]):
        url = f"https://www.xiaohongshu.com/{searched.group(0)}"
        xhs_id = searched.group("xhs_id")
        return await self.parse_explore(url, xhs_id)

    @handle(
        "hongshu.com/discovery/item/",
        r"discovery/item/(?P<xhs_id>[0-9a-zA-Z]+)\?[A-Za-z0-9._%&+=/#@-]*",
    )
    async def _parse_discovery(self, searched: re.Match[str]):
        route = searched.group(0)
        explore_route = route.replace("discovery/item", "explore", 1)
        xhs_id = searched.group("xhs_id")

        try:
            return await self.parse_explore(f"https://www.xiaohongshu.com/{explore_route}", xhs_id)
        except ParseException:
            logger.debug("parse_explore failed, fallback to parse_discovery")
            return await self.parse_discovery(f"https://www.xiaohongshu.com/{route}", xhs_id)

    async def parse_explore(self, url: str, xhs_id: str):
        # 修复：curl_cffi get
        resp = await self.client.get(url, headers=self.headers)
        html = resp.text
        logger.debug(f"url: {resp.url} | status: {resp.status_code}")

        json_obj = self._extract_initial_state_json(html)

        note_data = json_obj.get("note", {}).get("noteDetailMap", {}).get(xhs_id, {}).get("note", {})
        if not note_data:
            raise ParseException("can't find note detail in json_obj")

        return self._process_explore_data(note_data)

    async def parse_discovery(self, url: str, xhs_id: str | None = None):
        # 修复：curl_cffi get
        resp = await self.client.get(
            url,
            headers=self.ios_headers,
            allow_redirects=True,
        )
        html = resp.text

        json_obj = self._extract_initial_state_json(html)
        
        # 1. Try noteData (Discovery style)
        note_data = json_obj.get("noteData", {}).get("data", {}).get("noteData", {})
        if note_data:
            preload_data = json_obj.get("noteData", {}).get("normalNotePreloadData", {})
            return self._process_discovery_data(note_data, preload_data)

        # 2. Try note.noteDetailMap (Explore style)
        note_container = json_obj.get("note", {})
        detail_map = note_container.get("noteDetailMap", {})
        
        # 2.1 Try exact ID
        if xhs_id:
            note_data = detail_map.get(xhs_id, {}).get("note", {})
            if note_data:
                return self._process_explore_data(note_data)
        
        # 2.2 Try first item in detailMap
        if detail_map:
            first_key = next(iter(detail_map))
            note_data = detail_map[first_key].get("note", {})
            if note_data:
                logger.debug(f"Found note data in noteDetailMap using key: {first_key}")
                return self._process_explore_data(note_data)

        # 3. Try note.firstNote
        note_data = note_container.get("firstNote", {})
        if note_data:
             return self._process_explore_data(note_data)

        # 4. Try note.note
        note_data = note_container.get("note", {})
        if note_data:
             return self._process_explore_data(note_data)

        # Debug logging
        logger.warning(f"XHS Parse Failed. Keys in json_obj: {list(json_obj.keys())}")
        if "note" in json_obj:
            logger.warning(f"Keys in json_obj['note']: {list(json_obj['note'].keys())}")
            
        raise ParseException("解析异常: can't find noteData in noteData.data or noteDetailMap")

    def _process_explore_data(self, note_data: dict):
        """处理 Explore 风格的数据结构"""
        class Image(Struct):
            urlDefault: str

        class User(Struct):
            nickname: str
            avatar: str

        class NoteDetail(Struct):
            type: str
            title: str
            desc: str
            user: User
            imageList: list[Image] = field(default_factory=list)
            video: Video | None = None

            @property
            def nickname(self) -> str:
                return self.user.nickname

            @property
            def avatar_url(self) -> str:
                return self.user.avatar

            @property
            def image_urls(self) -> list[str]:
                return [item.urlDefault for item in self.imageList]

            @property
            def video_url(self) -> str | None:
                if self.type != "video" or not self.video:
                    return None
                return self.video.video_url

        note_detail = convert(note_data, type=NoteDetail)

        contents = []
        if video_url := note_detail.video_url:
            cover_url = note_detail.image_urls[0] if note_detail.image_urls else None
            contents.append(self.create_video_content(video_url, cover_url))

        elif image_urls := note_detail.image_urls:
            contents.extend(self.create_image_contents(image_urls))

        author = self.create_author(note_detail.nickname, note_detail.avatar_url)

        return self.result(
            title=note_detail.title,
            text=note_detail.desc,
            author=author,
            contents=contents,
        )

    def _process_discovery_data(self, note_data: dict, preload_data: dict):
        """处理 Discovery 风格的数据结构"""
        class Image(Struct):
            url: str
            urlSizeLarge: str | None = None

        class User(Struct):
            nickName: str
            avatar: str

        class NoteData(Struct):
            type: str
            title: str
            desc: str
            user: User
            time: int
            lastUpdateTime: int
            imageList: list[Image] = []
            video: Video | None = None

            @property
            def image_urls(self) -> list[str]:
                return [item.url for item in self.imageList]

            @property
            def video_url(self) -> str | None:
                if self.type != "video" or not self.video:
                    return None
                return self.video.video_url

        class NormalNotePreloadData(Struct):
            title: str
            desc: str
            imagesList: list[Image] = []

            @property
            def image_urls(self) -> list[str]:
                return [item.urlSizeLarge or item.url for item in self.imagesList]

        note_data_obj = convert(note_data, type=NoteData)

        contents = []
        if video_url := note_data_obj.video_url:
            if preload_data:
                preload_obj = convert(preload_data, type=NormalNotePreloadData)
                img_urls = preload_obj.image_urls
            else:
                img_urls = note_data_obj.image_urls
            contents.append(self.create_video_content(video_url, img_urls[0] if img_urls else None))
        elif img_urls := note_data_obj.image_urls:
            contents.extend(self.create_image_contents(img_urls))

        return self.result(
            title=note_data_obj.title,
            author=self.create_author(note_data_obj.user.nickName, note_data_obj.user.avatar),
            contents=contents,
            text=note_data_obj.desc,
            timestamp=note_data_obj.time // 1000,
        )

    def _extract_initial_state_json(self, html: str) -> dict[str, Any]:
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        matched = re.search(pattern, html)
        if not matched:
            raise ParseException("小红书分享链接失效或内容已删除")

        json_str = matched.group(1).replace("undefined", "null")
        return json.loads(json_str)


class Stream(Struct):
    h264: list[dict[str, Any]] | None = None
    h265: list[dict[str, Any]] | None = None
    av1: list[dict[str, Any]] | None = None
    h266: list[dict[str, Any]] | None = None


class Media(Struct):
    stream: Stream


class Video(Struct):
    media: Media

    @property
    def video_url(self) -> str | None:
        stream = self.media.stream
        if stream.h265:
            return stream.h265[0]["masterUrl"]
        elif stream.h264:
            return stream.h264[0]["masterUrl"]
        elif stream.av1:
            return stream.av1[0]["masterUrl"]
        elif stream.h266:
            return stream.h266[0]["masterUrl"]
        return None
