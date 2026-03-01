import asyncio
import re
from typing import TYPE_CHECKING, ClassVar

import msgspec
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ...data import VideoContent
from ...download import Downloader
from ..base import BaseParser, ParseException, Platform, handle
from .composer import DouyinMediaComposer
from .extractor import (
    extract_bgm_url,
    extract_dynamic_video_entries,
    extract_id_from_query,
    extract_router_data_json_str,
    pick_primary_aweme,
)

if TYPE_CHECKING:
    from ...data import ParseResult


class DouyinParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.composer = DouyinMediaComposer(downloader, config)
        self.cookies = ""
        ck_conf = config.get("cookies", {})
        if isinstance(ck_conf, dict):
            self.cookies = ck_conf.get("douyin_ck", "")
        if self.cookies:
            self._set_cookies(self.cookies)

    def _set_cookies(self, cookies: str):
        cleaned = cookies.replace("\n", "").replace("\r", "").strip()
        if cleaned:
            self.ios_headers["Cookie"] = cleaned
            self.android_headers["Cookie"] = cleaned

    def _update_cookies_from_response(self, response):
        pass

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}"

    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        short_url = f"https://{searched.group(0)}"
        final_url = await self.get_final_url(short_url, headers=self.ios_headers)
        try:
            keyword, m = self.search_url(final_url)
            return await self.parse(keyword, m)
        except Exception:
            vid = extract_id_from_query(final_url)
            if vid:
                return await self._parse_by_id_fallback(vid)
            raise ParseException(f"短链解析失败，无法识别最终链接: {final_url}")

    @handle("douyin", r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    async def _parse_douyin(self, searched: re.Match[str]):
        ty, vid = searched.group("ty"), searched.group("vid")
        if ty == "slides":
            return await self.parse_slides(vid)

        for url in (
            f"https://www.douyin.com/{ty}/{vid}",
            self._build_m_douyin_url(ty, vid),
            self._build_iesdouyin_url(ty, vid),
        ):
            try:
                return await self.parse_video(url, vid)
            except Exception:
                pass
        return await self._parse_with_ytdlp(vid)

    async def _parse_by_id_fallback(self, vid: str):
        for ty in ("video", "note"):
            for u in (self._build_m_douyin_url(ty, vid), self._build_iesdouyin_url(ty, vid)):
                try:
                    return await self.parse_video(u, vid)
                except Exception:
                    pass
        return await self._parse_with_ytdlp(vid)

    async def parse_video(self, url: str, vid: str):
        resp = await self.client.get(url, headers=self.ios_headers, allow_redirects=True, verify=False)
        if resp.status_code != 200:
            raise ParseException(f"页面请求失败 Status: {resp.status_code}")

        from .video import VideoData, recursive_collect_videos

        raw_data = msgspec.json.decode(extract_router_data_json_str(resp.text))
        targets = recursive_collect_videos(raw_data, prefer_vid=vid, limit=30)
        if not targets:
            raise ParseException("未找到 aweme 数据")

        aweme = pick_primary_aweme(targets, vid)

        # 动图优先
        entries = extract_dynamic_video_entries(aweme)
        if entries:
            merge_enabled = self.composer.as_bool(self.config.get("douyin_merge_dynamic_video", True), True)
            if merge_enabled and len(entries) > 1:
                bgm_url = extract_bgm_url(aweme)
                merged_task = asyncio.create_task(
                    self.composer.merge_dynamic_videos_with_bgm(entries, vid, bgm_url, self.ios_headers)
                )
                contents = [VideoContent(merged_task, None, duration=0)]
            else:
                contents = self.composer.build_unique_dynamic_contents_from_entries(entries, vid, self.ios_headers)

            meta = msgspec.convert(aweme, VideoData)
            author = self.create_author(meta.author.nickname, meta.avatar_url, ext_headers=self.ios_headers)
            return self.result(title=meta.desc, author=author, contents=contents, timestamp=meta.create_time)

        # 普通图/视频分支（按你原逻辑接回去）
        meta = msgspec.convert(aweme, VideoData)
        contents = []
        if meta.images and meta.image_urls:
            contents.extend(self.create_image_contents(meta.image_urls))
        elif meta.video_url:
            task = self.downloader.download_video(meta.video_url, video_name=f"douyin_{meta.id or vid}.mp4", ext_headers=self.ios_headers)
            contents.append(self.create_video_content(task, None, meta.video.duration if meta.video else 0))

        author = self.create_author(meta.author.nickname, meta.avatar_url, ext_headers=self.ios_headers)
        return self.result(title=meta.desc, author=author, contents=contents, timestamp=meta.create_time)

    async def parse_slides(self, video_id: str):
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {"aweme_ids": f"[{video_id}]", "request_source": "200"}
        resp = await self.client.get(url, params=params, headers=self.android_headers, verify=False)
        if resp.status_code >= 400:
            raise ParseException(f"API Error: {resp.status_code}")

        from .slides import SlidesInfo
        info = msgspec.json.decode(resp.content, type=SlidesInfo)
        if not info.aweme_details:
            raise ParseException("图集数据为空")
        slides = info.aweme_details[0]

        entries = [(f"idx:{i}:{u}", u.replace("playwm", "play")) for i, u in enumerate(slides.dynamic_urls or []) if u]
        if entries:
            merge_enabled = self.composer.as_bool(self.config.get("douyin_merge_dynamic_video", True), True)
            if merge_enabled and len(entries) > 1:
                merged_task = asyncio.create_task(
                    self.composer.merge_dynamic_videos_with_bgm(entries, video_id, None, self.android_headers)
                )
                contents = [VideoContent(merged_task, None, duration=0)]
            else:
                contents = self.composer.build_unique_dynamic_contents_from_entries(entries, video_id, self.android_headers)
        else:
            contents = self.create_image_contents(slides.image_urls or [])

        author = self.create_author(slides.name, slides.avatar_url, ext_headers=self.android_headers)
        return self.result(title=slides.desc, author=author, contents=contents, timestamp=slides.create_time)

    async def _parse_with_ytdlp(self, vid: str):
        url = f"https://www.douyin.com/video/{vid}"
        info = await self.downloader.ytdlp_extract_info(url)
        contents = []
        if info.duration:
            task = self.downloader.download_video(url, use_ytdlp=True, video_name=f"douyin_{vid}.mp4")
            contents.append(VideoContent(task, None, duration=info.duration))
        author = self.create_author(info.uploader or "抖音用户")
        return self.result(title=info.title or "抖音视频", text=info.description or "", author=author, contents=contents, timestamp=info.timestamp, url=url)
