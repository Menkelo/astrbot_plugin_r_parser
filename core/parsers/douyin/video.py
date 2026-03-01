from random import choice
from typing import Any
from urllib.parse import parse_qs, urlparse

from msgspec import Struct, field


def _stable_video_key_from_url(url: str) -> str:
    """
    从 URL 提取稳定去重 key，尽量忽略 host/query 噪音
    """
    try:
        p = urlparse(url)
        q = parse_qs(p.query)

        for k in ("video_id", "vid", "item_id", "aweme_id"):
            vals = q.get(k) or []
            if vals and vals[0]:
                return f"{k}:{vals[0]}"

        path = p.path.rstrip("/")
        if path:
            last = path.split("/")[-1]
            if last:
                return f"path:{last}"

        return f"url:{url}"
    except Exception:
        return f"url:{url}"


class Avatar(Struct):
    url_list: list[str] = field(default_factory=list)


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None


class PlayAddr(Struct):
    url_list: list[str] = field(default_factory=list)
    uri: str | None = None


class Cover(Struct):
    url_list: list[str] = field(default_factory=list)


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int = 0


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class VideoData(Struct):
    desc: str
    create_time: int
    author: Author

    aweme_id: str | None = None
    awemeId: str | None = None

    video: Video | None = None
    images: list[Image] | None = None

    @property
    def id(self) -> str:
        return self.aweme_id or self.awemeId or ""

    @property
    def image_urls(self) -> list[str]:
        if not self.images:
            return []
        # 图片保留随机无所谓
        return [choice(img.url_list) for img in self.images if img.url_list]

    @property
    def dynamic_video_items(self) -> list[tuple[str, str]]:
        """
        返回 [(dedupe_key, url), ...]
        关键修复：
        - 不再 random choice，改为稳定取第一个可用 url
        - key 优先用 play_addr.uri（最稳）
        - 无 uri 时退化到 image index + url，避免误去重导致漏发
        """
        if not self.images:
            return []

        out: list[tuple[str, str]] = []
        for idx, img in enumerate(self.images):
            if not img.video or not img.video.play_addr or not img.video.play_addr.url_list:
                continue

            raw_url = next((u for u in img.video.play_addr.url_list if isinstance(u, str) and u), None)
            if not raw_url:
                continue

            url = raw_url.replace("playwm", "play")
            uri = img.video.play_addr.uri
            key = f"uri:{uri}" if uri else f"idx:{idx}:{url}"
            out.append((key, url))

        return out

    @property
    def video_url(self) -> str | None:
        if not self.video or not self.video.play_addr.url_list:
            return None
        return choice(self.video.play_addr.url_list).replace("playwm", "play")

    @property
    def avatar_url(self) -> str | None:
        if self.author.avatar_thumb and self.author.avatar_thumb.url_list:
            return choice(self.author.avatar_thumb.url_list)
        if self.author.avatar_medium and self.author.avatar_medium.url_list:
            return choice(self.author.avatar_medium.url_list)
        return None

    @property
    def cover_url(self) -> str | None:
        if not self.video or not self.video.cover.url_list:
            return None
        return choice(self.video.cover.url_list)

    @property
    def avatar_url(self) -> str | None:
        if self.author.avatar_thumb and self.author.avatar_thumb.url_list:
            return choice(self.author.avatar_thumb.url_list)
        if self.author.avatar_medium and self.author.avatar_medium.url_list:
            return choice(self.author.avatar_medium.url_list)
        return None


def recursive_collect_videos(
    data: Any,
    prefer_vid: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """
    在任意 JSON(dict/list) 中递归收集有效 aweme 对象：
    - 包含 aweme_id/awemeId
    - 且包含 video 或 images 字段
    """
    found: list[dict] = []
    seen_ids: set[str] = set()

    def _walk(obj: Any):
        if len(found) >= limit:
            return

        if isinstance(obj, dict):
            curr_id = obj.get("aweme_id") or obj.get("awemeId")
            if curr_id is not None:
                sid = str(curr_id)
                if sid not in seen_ids and ("video" in obj or "images" in obj):
                    seen_ids.add(sid)
                    found.append(obj)

            for v in obj.values():
                _walk(v)

        elif isinstance(obj, list):
            for it in obj:
                _walk(it)

    _walk(data)

    if prefer_vid:
        found.sort(
            key=lambda x: 0 if str(x.get("aweme_id") or x.get("awemeId") or "") == prefer_vid else 1
        )

    return found


def recursive_search_video(data: Any, target_vid: str) -> dict | None:
    """
    兼容旧逻辑：返回首个匹配项
    """
    items = recursive_collect_videos(data, prefer_vid=target_vid, limit=1)
    return items[0] if items else None
