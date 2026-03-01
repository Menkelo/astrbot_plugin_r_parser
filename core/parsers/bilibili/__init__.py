import asyncio
import time
from pathlib import Path
from re import Match
from typing import ClassVar

from bilibili_api import Credential, request_settings, select_client
from bilibili_api.video import Video
from msgspec import convert

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ...constants import BILIBILI_HEADER
from ...data import MediaContent, Platform
from ...exception import SizeLimitException
from ...utils import ck2dict
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    handle,
)

# 你前面已经拆分过评论与码率模块，这里直接复用
from .comment_renderer import BiliCommentRenderer
from .comment_service import BiliCommentService
from .stream_selector import BiliStreamSelector


class BilibiliParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="bilibili", display_name="B站")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

        select_client("curl_cffi")
        request_settings.set("impersonate", "chrome120")
        request_settings.set("verify", False)

        self.headers = BILIBILI_HEADER.copy()
        self._credential: Credential | None = None
        self.cache_dir = Path(config["cache_dir"])

        self.bili_ck = config.get("cookies", {}).get("bili_ck", "")
        self.comment_limit = 9
        self.max_size_mb = config.get("performance", {}).get("source_max_size", 90)

        # 轻量缓存（提速）
        perf = config.get("performance", {})
        self._cache_ttl = int(perf.get("bili_cache_ttl", 120))
        self._video_info_cache: dict[str, tuple[float, dict]] = {}
        self._playurl_cache: dict[str, tuple[float, dict]] = {}

        # 评论过滤配置
        comment_conf = config.get("comment_filter", {})
        if not isinstance(comment_conf, dict):
            comment_conf = {}

        self.comment_renderer = BiliCommentRenderer()
        self.comment_service = BiliCommentService(
            parser=self,
            renderer=self.comment_renderer,
            comment_limit=self.comment_limit,
            enable_text_ad_filter=bool(comment_conf.get("enable_text_ad_filter", True)),
            enable_qr_filter=bool(comment_conf.get("enable_qr_filter", True)),
            qr_check_max=int(comment_conf.get("qr_check_max", 4)),
            qr_check_timeout=float(comment_conf.get("qr_check_timeout", 6)),
        )
        self.stream_selector = BiliStreamSelector()

    # region 路由处理

    @handle("b23.tv", r"b23\.tv/[A-Za-z\d\._?%&+\-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[A-Za-z\d\._?%&+\-=/#]+")
    async def _parse_short_link(self, searched: Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/BV", r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9a-zA-Z]{10})(?:\?p=(?P<page_num>\d{1,3}))?")
    async def _parse_bv(self, searched: Match[str]):
        bvid = str(searched.group("bvid"))
        page_num = int(searched.group("page_num") or 1)
        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/av", r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?:\?p=(?P<page_num>\d{1,3}))?")
    async def _parse_av(self, searched: Match[str]):
        avid = int(searched.group("avid"))
        page_num = int(searched.group("page_num") or 1)
        return await self.parse_video(avid=avid, page_num=page_num)

    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)")
    async def _parse_dynamic(self, searched: Match[str]):
        dynamic_id = int(searched.group("dynamic_id"))
        return await self.parse_dynamic(dynamic_id)

    @handle("opus", r"bilibili\.com/opus/(?P<dynamic_id>\d+)")
    async def _parse_opus(self, searched: Match[str]):
        dynamic_id = int(searched.group("dynamic_id"))
        return await self.parse_dynamic(dynamic_id)

    # endregion

    # region 缓存辅助

    def _cache_get(self, cache: dict[str, tuple[float, dict]], key: str) -> dict | None:
        item = cache.get(key)
        if not item:
            return None
        ts, val = item
        if time.time() - ts > self._cache_ttl:
            cache.pop(key, None)
            return None
        return val

    def _cache_set(self, cache: dict[str, tuple[float, dict]], key: str, val: dict):
        cache[key] = (time.time(), val)

    async def _get_video_info_cached(self, video: Video, cache_key: str) -> dict:
        if cached := self._cache_get(self._video_info_cache, cache_key):
            return cached
        data = await video.get_info()
        self._cache_set(self._video_info_cache, cache_key, data)
        return data

    async def _get_playurl_cached(self, video: Video, page_index: int, cache_key: str) -> dict:
        if cached := self._cache_get(self._playurl_cache, cache_key):
            return cached
        data = await video.get_download_url(page_index=page_index)
        self._cache_set(self._playurl_cache, cache_key, data)
        return data

    # endregion

    # region CDN候选增强（关键修复）

    @staticmethod
    def _collect_stream_urls(item: dict) -> list[str]:
        urls: list[str] = []

        base = item.get("baseUrl") or item.get("base_url")
        if isinstance(base, str) and base:
            urls.append(base)

        backups = item.get("backupUrl") or item.get("backup_url") or []
        if isinstance(backups, list):
            for u in backups:
                if isinstance(u, str) and u:
                    urls.append(u)

        # 去重保序
        seen = set()
        uniq = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)

        # 优先非 mcdn / 非 8082
        def score(u: str) -> tuple[int, int]:
            bad_port = 1 if ":8082" in u else 0
            bad_host = 1 if "mcdn" in u else 0
            return (bad_port, bad_host)

        uniq.sort(key=score)
        return uniq

    def _select_best_stream_candidates(self, data: dict, duration: int, limit_mb: int) -> tuple[list[str], list[str]]:
        """
        返回 (video_candidates, audio_candidates)
        """
        if "dash" not in data:
            if "durl" in data and data["durl"]:
                u = data["durl"][0].get("url")
                return ([u] if u else []), []
            return [], []

        dash = data["dash"]
        video_streams = [v for v in dash.get("video", []) if v.get("id", 0) <= 64]
        audio_streams = dash.get("audio", [])

        if not video_streams:
            return [], []

        audio_size_mb = 0.0
        audio_candidates: list[str] = []

        if audio_streams:
            best_audio = audio_streams[0]
            audio_candidates = self._collect_stream_urls(best_audio)
            bandwidth = best_audio.get("bandwidth", 128000)
            audio_size_mb = (bandwidth / 8 * duration) / 1024 / 1024

        remaining_mb = max(limit_mb - audio_size_mb, 0)

        video_streams.sort(key=lambda x: x.get("id", 0), reverse=True)
        selected_item = None

        for v in video_streams:
            bandwidth = v.get("bandwidth", 0)
            est_size_mb = (bandwidth / 8 * duration) / 1024 / 1024
            if est_size_mb * 1.25 <= remaining_mb:
                selected_item = v
                break

        if selected_item is None:
            selected_item = video_streams[-1]

        video_candidates = self._collect_stream_urls(selected_item)
        return video_candidates, audio_candidates

    # endregion

    async def parse_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
    ):
        from .video import VideoInfo

        video = await self._get_video(bvid=bvid, avid=avid)

        try:
            key = f"bvid:{bvid}" if bvid else f"avid:{avid}"
            raw_info = await self._get_video_info_cached(video, key)
        except Exception as e:
            logger.error(f"[Bilibili] get_info error: {e}")
            raise ParseException(f"B站 API 请求失败: {e}")

        video_info = convert(raw_info, VideoInfo)
        page_info = video_info.extract_info_with_page(page_num)

        text = f"简介: {video_info.desc}" if video_info.desc else None
        author = self.create_author(video_info.owner.name, avatar_url=None)

        url = f"https://bilibili.com/{video_info.bvid}"
        url += f"?p={page_info.index + 1}" if page_info.index > 0 else ""

        limit_mb = self.max_size_mb

        task_play_url = self._get_playurl_cached(
            video, page_info.index, f"{video_info.bvid}:{page_info.index}"
        )
        task_comments = self.comment_service.build_comment_image_content(
            video_info.aid,
            1,
            video_title=page_info.title,
            video_cover=page_info.cover,
        )
        play_url_data, comment_imgs = await asyncio.gather(task_play_url, task_comments)

        v_candidates, a_candidates = self._select_best_stream_candidates(
            play_url_data, page_info.duration, limit_mb
        )

        if not v_candidates:
            raise SizeLimitException(f"即使是最低画质也超过了限制 ({limit_mb}MB)")

        async def download_video_task():
            output_path = self.cache_dir / f"{video_info.bvid}-{page_num}.mp4"
            if output_path.exists():
                if output_path.stat().st_size > 100:
                    return output_path
                try:
                    output_path.unlink(missing_ok=True)
                except Exception:
                    pass

            download_headers = self.headers.copy()
            download_headers["Referer"] = url

            last_err: Exception | None = None

            # 先尝试音视频合并候选
            if a_candidates:
                for v_url in v_candidates:
                    for a_url in a_candidates:
                        try:
                            return await self.downloader.download_av_and_merge(
                                v_url,
                                a_url,
                                output_path=output_path,
                                ext_headers=download_headers,
                                max_size_mb=limit_mb,
                            )
                        except Exception as e:
                            last_err = e
                            continue

            # 再尝试纯视频候选
            for v_url in v_candidates:
                try:
                    return await self.downloader.streamd(
                        v_url,
                        file_name=output_path.name,
                        ext_headers=download_headers,
                        max_size_mb=limit_mb,
                    )
                except Exception as e:
                    last_err = e
                    continue

            raise ParseException(f"B站媒体下载失败（已尝试全部CDN候选）: {last_err}")

        video_content = self.create_video_content(
            asyncio.create_task(download_video_task(), name=f"bili_dl_{video_info.bvid}_{page_num}"),
            cover_url=None,
            duration=page_info.duration,
        )
        video_content.is_file_upload = False

        return self.result(
            url=url,
            title=page_info.title,
            timestamp=page_info.timestamp,
            text=text,
            author=author,
            contents=[video_content],
            comment_contents=comment_imgs,
        )

    async def parse_dynamic(self, dynamic_id: int):
        from bilibili_api.dynamic import Dynamic
        from .dynamic import DynamicItem

        dynamic = Dynamic(dynamic_id, await self.credential)
        dynamic_data = convert(await dynamic.get_info(), DynamicItem)
        dynamic_info = dynamic_data.item
        author = self.create_author(dynamic_info.name, avatar_url=None)

        contents: list[MediaContent] = []
        if dynamic_info.image_urls:
            contents = self.create_image_contents(dynamic_info.image_urls)

        return self.result(
            title=dynamic_info.title,
            text=dynamic_info.text,
            timestamp=dynamic_info.timestamp,
            author=author,
            contents=contents,
        )

    async def _get_video(self, *, bvid: str | None = None, avid: int | None = None) -> Video:
        if avid:
            return Video(aid=avid, credential=await self.credential)
        if bvid:
            return Video(bvid=bvid, credential=await self.credential)
        raise ParseException("avid 和 bvid 至少指定一项")

    async def _init_credential(self):
        if not self.bili_ck:
            return
        try:
            self._credential = Credential.from_cookies(ck2dict(self.bili_ck))
        except Exception as e:
            logger.warning(f"Cookie加载失败: {e}")

    @property
    async def credential(self) -> Credential | None:
        if self._credential is None:
            await self._init_credential()
        return self._credential
