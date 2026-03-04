import hashlib
import json
import random
from pathlib import Path

from astrbot.api import logger

from ...data import ImageContent
from ..base import ParseException


class BiliLiveService:
    def __init__(self, parser):
        self.parser = parser

    async def _get_json(self, url: str, params: dict, room_id: int, retry: int = 3):
        headers = self.parser.headers.copy()
        headers["Referer"] = f"https://live.bilibili.com/{room_id}"
        if self.parser.bili_ck:
            headers["Cookie"] = self.parser.bili_ck

        last = {}
        for i in range(retry):
            try:
                resp = await self.parser.http_get(
                    url,
                    headers=headers,
                    params=params,
                    allow_redirects=True,
                    timeout=8,
                )
                if hasattr(resp, "json"):
                    try:
                        data = resp.json()
                        last = data if isinstance(data, dict) else {}
                    except Exception:
                        last = {}
                else:
                    txt = getattr(resp, "text", "") or ""
                    last = json.loads(txt) if txt else {}

                code = last.get("code")
                logger.info(
                    f"[Bilibili-live] api={url} try={i+1}/{retry} code={code} msg={last.get('msg') or last.get('message')}"
                )
                if code == 0:
                    return last
                if code in (-352, -412, -799):
                    await __import__("asyncio").sleep((i + 1) * 1.0 + random.uniform(0.1, 0.4))
                    continue
                return last
            except Exception as e:
                logger.info(f"[Bilibili-live] api={url} try={i+1}/{retry} ex={e}")
                await __import__("asyncio").sleep((i + 1) * 0.6)
        return last

    async def _fetch_live_html_info(self, rid: int):
        url = f"https://live.bilibili.com/{rid}"
        headers = self.parser.headers.copy()
        headers["Referer"] = url
        if self.parser.bili_ck:
            headers["Cookie"] = self.parser.bili_ck

        try:
            resp = await self.parser.http_get(url, headers=headers, allow_redirects=True, timeout=10)
            html = getattr(resp, "text", "") or ""
        except Exception as e:
            logger.info(f"[Bilibili-live] html fetch ex={e}")
            return {}

        if not html:
            return {}

        def _extract_json_by_marker(text: str, marker: str) -> dict:
            idx = text.find(marker)
            if idx < 0:
                return {}
            start = text.find("{", idx)
            if start < 0:
                return {}

            depth = 0
            end = -1
            in_str = False
            esc = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                else:
                    if ch == '"':
                        in_str = True
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break

            if end < 0:
                return {}
            try:
                return json.loads(text[start:end])
            except Exception:
                return {}

        data = _extract_json_by_marker(html, "__NEPTUNE_IS_MY_WAIFU__=")
        if not data:
            data = _extract_json_by_marker(html, "__INITIAL_STATE__=")
        if not data:
            return {}

        room = {}
        anchor = {}

        if isinstance(data.get("roomInfoRes"), dict):
            room = (data["roomInfoRes"].get("data") or {}).get("room_info") or {}
            anchor = (data["roomInfoRes"].get("data") or {}).get("anchor_info") or {}
            if isinstance(anchor, dict):
                anchor = anchor.get("base_info") or anchor

        if not room and isinstance(data.get("room_info"), dict):
            room = data.get("room_info") or {}
        if not anchor and isinstance(data.get("anchor_info"), dict):
            anchor = (data.get("anchor_info") or {}).get("base_info") or {}

        return {"room_info": room, "anchor_base": anchor}

    async def parse_live(self, room_id: int):
        init_api = "https://api.live.bilibili.com/room/v1/Room/room_init"
        info_api = "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom"
        fallback_info_api = "https://api.live.bilibili.com/room/v1/Room/get_info"

        init_raw = await self._get_json(init_api, {"id": room_id}, room_id)
        if init_raw.get("code") != 0:
            real_room_id = room_id
            live_status = 0
        else:
            init_data = init_raw.get("data") or {}
            real_room_id = int(init_data.get("room_id") or room_id)
            live_status = int(init_data.get("live_status") or 0)

        room_info = {}
        anchor_base = {}

        info_raw = await self._get_json(info_api, {"room_id": real_room_id}, real_room_id)
        if info_raw.get("code") == 0:
            data = info_raw.get("data") or {}
            room_info = data.get("room_info") or {}
            anchor_base = ((data.get("anchor_info") or {}).get("base_info") or {})
        else:
            fb_raw = await self._get_json(fallback_info_api, {"room_id": real_room_id}, real_room_id)
            if fb_raw.get("code") == 0:
                room_info = fb_raw.get("data") or {}
            else:
                html_data = await self._fetch_live_html_info(real_room_id)
                room_info = html_data.get("room_info") or {}
                anchor_base = html_data.get("anchor_base") or {}

        title = room_info.get("title") or f"B站直播间 {real_room_id}"
        uname = anchor_base.get("uname") or "B站主播"
        cover = room_info.get("cover") or room_info.get("keyframe")
        avatar = anchor_base.get("face")
        online = room_info.get("online")
        parent_area = room_info.get("parent_area_name") or ""
        area = room_info.get("area_name") or ""
        area_text = f"{parent_area} / {area}".strip(" /")

        digest = hashlib.md5(
            f"{real_room_id}|{title}|{uname}|{live_status}|{online}|live_service_v1".encode()
        ).hexdigest()[:10]
        out_path = Path(self.parser.cache_dir) / f"bili_live_{real_room_id}_{digest}.png"

        if not out_path.exists():
            await self.parser.live_renderer.render_live_card(
                out_path=out_path,
                title=title,
                uname=uname,
                room_id=real_room_id,
                cover=cover,
                avatar=avatar,
                live_status=live_status,
                area_text=area_text,
                online=online if isinstance(online, int) else None,
            )

        return self.parser.result(
            contents=[ImageContent(out_path)],
            extra={"force_direct_media": True},
        )
