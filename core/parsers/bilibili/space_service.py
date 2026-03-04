import hashlib
import time
from pathlib import Path

from astrbot.api import logger

from ...data import ImageContent
from ..base import ParseException


class BiliSpaceService:
    EP_CARD = "https://api.bilibili.com/x/web-interface/card"
    EP_REL = "https://api.bilibili.com/x/relation/stat"
    EP_TOP_ARC = "https://api.bilibili.com/x/space/top/arc"
    EP_ARC_SEARCH = "https://api.bilibili.com/x/space/arc/search"

    def __init__(self, parser):
        self.parser = parser
        perf = parser.config.get("performance", {})
        self._ttl = int(perf.get("bili_space_cache_ttl", 180))
        self._rep_cache: dict[int, tuple[float, dict | None]] = {}

    async def _get_json(self, url: str, params: dict, mid: int, timeout: int = 8) -> dict:
        headers = self.parser.headers.copy()
        headers["Referer"] = f"https://space.bilibili.com/{mid}"
        try:
            resp = await self.parser.http_get(
                url,
                headers=headers,
                params=params,
                allow_redirects=True,
                timeout=timeout,
            )
            if hasattr(resp, "json"):
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
            txt = getattr(resp, "text", None)
            if txt:
                import json
                data = json.loads(txt)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    @staticmethod
    def _norm_cover(url: str | None) -> str | None:
        if not url:
            return None
        return f"https:{url}" if url.startswith("//") else url

    @staticmethod
    def _fmt_date(ts) -> str | None:
        try:
            ts = int(ts)
            if ts <= 0:
                return None
            return time.strftime("%Y-%m-%d", time.localtime(ts))
        except Exception:
            return None

    @staticmethod
    def _play_val(v: dict) -> int:
        p = v.get("play", 0)
        try:
            return int(p)
        except Exception:
            # 有些接口会给字符串，比如 "--"
            return 0

    def _to_work(self, v: dict) -> dict | None:
        if not isinstance(v, dict):
            return None
        bvid = v.get("bvid")
        aid = v.get("aid")
        url = f"https://www.bilibili.com/video/{bvid}" if bvid else (f"https://www.bilibili.com/video/av{aid}" if aid else None)
        if not url:
            return None
        ts = v.get("created") or v.get("pubdate") or v.get("ctime") or 0
        return {
            "title": v.get("title") or "未命名稿件",
            "cover": self._norm_cover(v.get("pic") or v.get("cover")),
            "url": url,
            "ts": int(ts) if str(ts).isdigit() else 0,
            "date": self._fmt_date(ts),
        }

    @staticmethod
    def _cache_get(cache: dict[int, tuple[float, dict | None]], key: int, ttl: int) -> dict | None:
        item = cache.get(key)
        if not item:
            return None
        ts, val = item
        if time.time() - ts > ttl:
            cache.pop(key, None)
            return None
        return val

    @staticmethod
    def _cache_set(cache: dict[int, tuple[float, dict | None]], key: int, val: dict | None):
        cache[key] = (time.time(), val)

    async def _fetch_profile(self, mid: int) -> dict:
        profile = {
            "name": f"UP主 {mid}",
            "avatar": None,
            "sign": "",
            "level": None,
            "official_title": None,
            "following": None,
            "follower": None,
            "archive_count": None,
        }

        card = await self._get_json(self.EP_CARD, {"mid": mid}, mid)
        if card.get("code") == 0:
            data = card.get("data") or {}
            c = data.get("card") or {}
            profile["name"] = c.get("name") or profile["name"]
            profile["avatar"] = self._norm_cover(c.get("face"))
            profile["sign"] = c.get("sign") or ""
            profile["level"] = (c.get("level_info") or {}).get("current_level")
            profile["official_title"] = (
                (c.get("Official") or {}).get("title")
                or (c.get("official") or {}).get("title")
                or None
            )
            profile["following"] = data.get("following")
            profile["follower"] = data.get("follower")
            profile["archive_count"] = data.get("archive_count")
        else:
            raise ParseException(f"空间信息获取失败: {card.get('message') or card.get('code')}")

        rel = await self._get_json(self.EP_REL, {"vmid": mid}, mid)
        if rel.get("code") == 0:
            d = rel.get("data") or {}
            if isinstance(d.get("following"), int):
                profile["following"] = d.get("following")
            if isinstance(d.get("follower"), int):
                profile["follower"] = d.get("follower")

        return profile

    async def _fetch_representative(self, mid: int) -> dict | None:
        # 1) 置顶代表作
        top = await self._get_json(self.EP_TOP_ARC, {"vmid": mid}, mid)
        if top.get("code") == 0:
            data = top.get("data") or {}
            arc = data.get("archive") if isinstance(data.get("archive"), dict) else data
            rep = self._to_work(arc)
            if rep:
                return rep

        # 2) 无置顶时，取“最高播放量”兜底
        search = await self._get_json(
            self.EP_ARC_SEARCH,
            {
                "mid": mid,
                "pn": 1,
                "ps": 30,          # 拉多一点，本地挑最高播放更稳
                "tid": 0,
                "keyword": "",
                "order": "click",
            },
            mid,
        )

        if search.get("code") == 0:
            vlist = (((search.get("data") or {}).get("list") or {}).get("vlist") or [])
            if vlist:
                best = max(vlist, key=self._play_val)
                rep = self._to_work(best)
                if rep:
                    return rep

        logger.info(
            f"[Bilibili] representative not found mid={mid}, "
            f"code={search.get('code')}, msg={search.get('message')}"
        )
        return None

    async def parse_space(self, mid: int):
        profile = await self._fetch_profile(mid)

        rep = self._cache_get(self._rep_cache, mid, self._ttl)
        if rep is None:
            rep = await self._fetch_representative(mid)
            self._cache_set(self._rep_cache, mid, rep)

        digest = hashlib.md5(
            f"{mid}|{profile['name']}|{profile['sign']}|{profile['following']}|{profile['follower']}|{profile['archive_count']}|{rep}|rep_simple_v2".encode()
        ).hexdigest()[:10]
        out_path = Path(self.parser.cache_dir) / f"bili_space_{mid}_{digest}.png"

        if not out_path.exists():
            await self.parser.space_renderer.render_space_card(
                out_path=out_path,
                name=profile["name"],
                mid=mid,
                avatar=profile["avatar"],
                sign=profile["sign"],
                level=profile["level"],
                official_title=profile["official_title"],
                following=profile["following"],
                follower=profile["follower"],
                archive_count=profile["archive_count"],
                representative_work=rep,
            )

        return self.parser.result(
            contents=[ImageContent(out_path)],
            extra={"force_direct_media": True},
        )
