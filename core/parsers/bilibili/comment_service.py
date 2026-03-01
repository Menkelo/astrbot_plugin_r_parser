import asyncio
import hashlib
import re
from pathlib import Path

from msgspec import json as msgjson

from astrbot.api import logger

from ...data import ImageContent
from .comment_renderer import BiliCommentRenderer


class BiliCommentService:
    def __init__(
        self,
        parser,
        renderer: BiliCommentRenderer,
        *,
        comment_limit: int = 9,
        enable_text_ad_filter: bool = True,
        enable_qr_filter: bool = True,
        qr_check_max: int = 4,
        qr_check_timeout: float = 6.0,
    ):
        self.parser = parser
        self.renderer = renderer
        self.comment_limit = comment_limit
        self.enable_text_ad_filter = enable_text_ad_filter
        self.enable_qr_filter = enable_qr_filter
        self.qr_check_max = qr_check_max
        self.qr_check_timeout = qr_check_timeout

        self._ad_kw_re = re.compile(
            r"(微信|v信|vx|加微|私信|进群|福利|代理|兼职|看片|资源|加我|联系我|返利|推广|引流|合作)",
            re.IGNORECASE,
        )
        self._contact_re = re.compile(
            r"(wx[:：]?\s*[a-zA-Z][-_a-zA-Z0-9]{4,}|qq[:：]?\s*\d{5,}|tg[:：]?\s*[a-zA-Z0-9_]{4,})",
            re.IGNORECASE,
        )
        self._shortlink_re = re.compile(
            r"(https?://)?([a-zA-Z0-9-]+\.)?(t\.cn|u\.jd\.com|dwz\.cn|v\.douyin\.com|b23\.tv)/",
            re.IGNORECASE,
        )

        self._qr_detect_cache: dict[str, bool] = {}

    @property
    def headers(self) -> dict[str, str]:
        return self.parser.headers

    @property
    def cache_dir(self) -> Path:
        return self.parser.cache_dir

    @property
    def client(self):
        return self.parser.client

    def _is_ad_like_text(self, text: str) -> bool:
        if not text:
            return False
        return bool(
            self._ad_kw_re.search(text)
            or self._contact_re.search(text)
            or self._shortlink_re.search(text)
        )

    async def _has_qr_in_image(self, img_url: str) -> bool:
        if not img_url:
            return False

        if img_url in self._qr_detect_cache:
            return self._qr_detect_cache[img_url]

        if len(self._qr_detect_cache) > 512:
            self._qr_detect_cache.clear()

        try:
            resp = await self.client.get(
                img_url,
                headers=self.headers,
                timeout=self.qr_check_timeout,
            )
            if resp.status_code != 200 or not resp.content:
                self._qr_detect_cache[img_url] = False
                return False

            try:
                import cv2
                import numpy as np

                arr = np.frombuffer(resp.content, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    self._qr_detect_cache[img_url] = False
                    return False

                detector = cv2.QRCodeDetector()
                decoded, points, _ = detector.detectAndDecode(img)
                has_qr = bool(decoded) or points is not None
                self._qr_detect_cache[img_url] = has_qr
                return has_qr
            except Exception:
                # 未安装 cv2 / 解析失败时，降级为不过滤
                self._qr_detect_cache[img_url] = False
                return False

        except Exception:
            self._qr_detect_cache[img_url] = False
            return False

    async def _should_skip_comment(
        self,
        message: str,
        pic_url: str | None,
        qr_check_counter: list[int],
    ) -> bool:
        # 第一层：文本广告/引流过滤
        if self.enable_text_ad_filter and self._is_ad_like_text(message):
            return True

        # 第二层：二维码图片过滤
        if (
            self.enable_qr_filter
            and pic_url
            and qr_check_counter[0] < self.qr_check_max
            and (not message or len(message.strip()) <= 8)
        ):
            qr_check_counter[0] += 1
            if await self._has_qr_in_image(pic_url):
                return True

        return False

    async def build_comment_image_content(
        self,
        oid: int,
        type_: int,
        *,
        video_title: str,
        video_cover: str | None,
    ) -> list[ImageContent]:
        """
        抓取评论并返回 [ImageContent(...)]，渲染采用延迟任务，不阻塞主流程。
        """
        url = "https://api.bilibili.com/x/v2/reply/main"
        strict_list = []
        relaxed_list = []
        seen = set()

        next_cursor = 0
        is_end = False
        max_pages = 5

        qr_check_counter = [0]

        for _ in range(max_pages):
            if is_end or len(strict_list) >= self.comment_limit:
                break

            params = {
                "oid": oid,
                "type": type_,
                "mode": 3,
                "next": next_cursor,
                "ps": 20,
            }

            try:
                resp = await self.client.get(url, params=params, headers=self.headers, timeout=5)
                if resp.status_code != 200:
                    break

                data = msgjson.decode(resp.content)
                if data.get("code") != 0:
                    break

                block = data.get("data") or {}
                replies = block.get("replies") or []
                cursor = block.get("cursor") or {}
                is_end = bool(cursor.get("is_end"))
                next_cursor = cursor.get("next", next_cursor + 1)

                for item in replies:
                    rpid = item.get("rpid")
                    if rpid in seen:
                        continue
                    seen.add(rpid)

                    content = item.get("content", {})
                    member = item.get("member", {})
                    raw_msg = content.get("message") or ""
                    message = re.sub(r"\[.*?\]", "", raw_msg).strip()

                    pics = content.get("pictures") or []
                    pic_url = pics[0].get("img_src") if pics else None

                    if not message and not pic_url:
                        continue

                    if await self._should_skip_comment(message, pic_url, qr_check_counter):
                        continue

                    data_obj = {
                        "avatar": member.get("avatar", ""),
                        "uname": member.get("uname", ""),
                        "message": message,
                        "pic": pic_url,
                    }

                    if "@" in message:
                        relaxed_list.append(data_obj)
                    else:
                        strict_list.append(data_obj)

            except Exception as e:
                logger.warning(f"[Bilibili] 评论抓取错误: {e}")
                break

        if len(strict_list) < self.comment_limit:
            need = self.comment_limit - len(strict_list)
            strict_list.extend(relaxed_list[:need])

        comments_data = strict_list[:self.comment_limit]
        if not comments_data:
            return []

        c_hash = hashlib.md5(
            f"{comments_data[0]}{self.comment_limit}_relax".encode()
        ).hexdigest()[:8]
        out_path = self.cache_dir / f"bili_comments_merged_{oid}_{c_hash}.png"

        if out_path.exists():
            return [ImageContent(out_path)]

        async def _render_then_return():
            await self.renderer.render_merged_comments(
                out_path=out_path,
                comments=comments_data,
                video_title=video_title,
                video_cover=video_cover,
            )
            return out_path

        return [
            ImageContent(
                asyncio.create_task(_render_then_return(), name=f"bili_comment_render_{oid}")
            )
        ]
