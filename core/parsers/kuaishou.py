import re
import asyncio
from random import choice
from typing import ClassVar, TypeAlias

import msgspec
from msgspec import Struct, field

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import Platform, VideoContent
from ..download import Downloader
from .base import BaseParser, ParseException, handle


class KuaiShouParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="kuaishou", display_name="快手")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.ios_headers.update({
            "Referer": "https://v.kuaishou.com/",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1"
        })

    @handle("v.kuaishou", r"v\.kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
    @handle("kuaishou", r"(?:www\.)?kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
    @handle("chenzhongtech", r"(?:v\.m\.)?chenzhongtech\.com/fw/[A-Za-z\d._?%&+\-=/#]+")
    async def _parse_v_kuaishou(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        
        real_url = None
        last_err = None
        
        for i in range(3):
            try:
                resp = await self.client.get(
                    url, 
                    headers=self.ios_headers, 
                    allow_redirects=False, 
                    timeout=30
                )
                if resp.status_code in (301, 302):
                    real_url = resp.headers.get("Location")
                else:
                    real_url = str(resp.url)
                
                if real_url:
                    break
            except Exception as e:
                last_err = e
                logger.debug(f"[快手] 获取重定向失败 (尝试 {i+1}/3): {e}")
                await asyncio.sleep(1)

        if not real_url:
            raise ParseException(f"获取重定向失败: {last_err}")

        real_url = real_url.replace("/fw/long-video/", "/fw/photo/")
        logger.debug(f"[快手] 目标页面: {real_url}")

        response_text = ""
        try:
            resp = await self.client.get(real_url, headers=self.ios_headers, timeout=30)
            if resp.status_code >= 400:
                raise ParseException(f"获取页面失败 {resp.status_code}")
            response_text = resp.text
        except Exception as e:
            raise ParseException(f"网络请求失败: {e}")

        try:
            pattern = r"window\.INIT_STATE\s*=\s*(.*?)</script>"
            matched = re.search(pattern, response_text)
            if matched:
                json_str = matched.group(1).strip()
                init_state = msgspec.json.decode(json_str, type=KuaishouInitState)
                photo = next(
                    (d.photo for d in init_state.values() if d.photo is not None), None
                )
                if photo:
                    return self._build_result_from_photo(photo)
        except Exception as e:
            logger.debug(f"[快手] INIT_STATE 解析失败: {e}，尝试正则提取")

        video_url = None
        if match := re.search(r'"srcNoMark":"(https?://[^"]+)"', response_text):
            video_url = match.group(1).encode('utf-8').decode('unicode_escape')
        elif match := re.search(r'"src":"(https?://[^"]+)"', response_text):
            video_url = match.group(1).encode('utf-8').decode('unicode_escape')
            
        if video_url:
            title = "快手视频"
            if match := re.search(r'"caption":"([^"]+)"', response_text):
                try: title = match.group(1).encode('utf-8').decode('unicode_escape')
                except: pass
            
            author = "快手用户"
            if match := re.search(r'"userName":"([^"]+)"', response_text):
                try: author = match.group(1).encode('utf-8').decode('unicode_escape')
                except: pass
                
            # 提纯：手动设置为 None
            cover = None
            
            contents = [VideoContent(
                self.downloader.download_video(video_url, ext_headers=self.ios_headers),
                None # cover task
            )]
            
            return self.result(
                title=title,
                author=self.create_author(author),
                contents=contents,
                url=real_url
            )

        raise ParseException("快手解析失败: 未找到视频信息")

    def _build_result_from_photo(self, photo):
        contents = []
        if video_url := photo.video_url:
            contents.append(
                # BaseParser 已修改，create_video_content 会自动忽略传入的 cover
                self.create_video_content(
                    video_url, photo.cover_url, photo.duration, ext_headers=self.ios_headers
                )
            )
        if img_urls := photo.img_urls:
            contents.extend(
                self.create_image_contents(img_urls, ext_headers=self.ios_headers)
            )

        author = self.create_author(
            photo.name, photo.head_url, ext_headers=self.ios_headers
        )

        return self.result(
            title=photo.caption,
            author=author,
            contents=contents,
            timestamp=photo.timestamp // 1000,
        )


class CdnUrl(Struct):
    cdn: str
    url: str | None = None


class Atlas(Struct):
    music_cdn_list: list[CdnUrl] = field(name="musicCdnList", default_factory=list)
    cdn_list: list[CdnUrl] = field(name="cdnList", default_factory=list)
    size: list[dict] = field(name="size", default_factory=list)
    img_route_list: list[str] = field(name="list", default_factory=list)

    @property
    def img_urls(self):
        if len(self.cdn_list) == 0 or len(self.img_route_list) == 0:
            return []
        cdn = choice(self.cdn_list).cdn
        return [f"https://{cdn}/{url}" for url in self.img_route_list]


class ExtParams(Struct):
    atlas: Atlas = field(default_factory=Atlas)


class Photo(Struct):
    caption: str
    timestamp: int
    duration: int = 0
    user_name: str = field(default="未知用户", name="userName")
    head_url: str | None = field(default=None, name="headUrl")
    cover_urls: list[CdnUrl] = field(name="coverUrls", default_factory=list)
    main_mv_urls: list[CdnUrl] = field(name="mainMvUrls", default_factory=list)
    ext_params: ExtParams = field(name="ext_params", default_factory=ExtParams)

    @property
    def name(self) -> str:
        return self.user_name.replace("\u3164", "").strip()

    @property
    def cover_url(self):
        return choice(self.cover_urls).url if len(self.cover_urls) != 0 else None

    @property
    def video_url(self):
        return choice(self.main_mv_urls).url if len(self.main_mv_urls) != 0 else None

    @property
    def img_urls(self):
        return self.ext_params.atlas.img_urls


class TusjohData(Struct):
    result: int
    photo: Photo | None = None


KuaishouInitState: TypeAlias = dict[str, TusjohData]
