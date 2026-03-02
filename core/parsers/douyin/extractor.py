import re
from urllib.parse import parse_qs, urlparse


def extract_id_from_query(url: str) -> str | None:
    """
    兼容旧函数名：
    - 先查 query
    - 再查 path（关键修复）
    """
    try:
        p = urlparse(url)
        query = parse_qs(p.query)
    except Exception:
        return None

    # 1) query 参数里取
    for key in ("modal_id", "aweme_id", "item_id", "video_id", "note_id", "id"):
        vals = query.get(key) or []
        for v in vals:
            if v and str(v).isdigit():
                return str(v)

    # 2) path 里取（关键补充）
    path = p.path or ""

    # /share/video/761226...
    m = re.search(r"/share/(?:video|note|slides)/(\d+)", path)
    if m:
        return m.group(1)

    # /video/761226... 或 /note/761226...
    m = re.search(r"/(?:video|note)/(\d+)", path)
    if m:
        return m.group(1)

    # 兜底：路径最后一个纯数字段
    m = re.search(r"/(\d+)(?:/)?$", path)
    if m:
        return m.group(1)

    return None


def extract_router_data_json_str(html: str) -> str:
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", html, re.DOTALL)
    if not m:
        raise ValueError("未在页面 HTML 中找到 _ROUTER_DATA")
    s = m.group(1).strip()
    if s.endswith(";"):
        s = s[:-1].strip()
    return s


def pick_primary_aweme(targets: list[dict], vid: str) -> dict:
    for obj in targets:
        oid = str(obj.get("aweme_id") or obj.get("awemeId") or "")
        if oid == vid:
            return obj
    return targets[0]


def extract_dynamic_video_entries(aweme_obj: dict) -> list[tuple[str, str]]:
    """
    返回 [(dedupe_key, video_url), ...]
    """
    from urllib.parse import parse_qs, urlparse

    entries: list[tuple[str, str]] = []
    images = aweme_obj.get("images") or []
    if not isinstance(images, list):
        return entries

    for idx, img in enumerate(images):
        if not isinstance(img, dict):
            continue
        video = img.get("video") or {}
        play_addr = video.get("play_addr") or {}
        url_list = play_addr.get("url_list") or []
        if not isinstance(url_list, list) or not url_list:
            continue

        raw = next((u for u in url_list if isinstance(u, str) and u), None)
        if not raw:
            continue
        url = raw.replace("playwm", "play")

        uri = play_addr.get("uri")
        if uri:
            key = f"uri:{uri}"
        else:
            q = parse_qs(urlparse(url).query)
            video_id = (q.get("video_id") or q.get("vid") or [""])[0]
            key = f"vid:{video_id}" if video_id else f"idx:{idx}:{url}"

        entries.append((key, url))

    return entries


def extract_bgm_url(aweme_obj: dict) -> str | None:
    music = aweme_obj.get("music") or {}
    play_url = music.get("play_url") or {}
    url_list = play_url.get("url_list") or []
    for u in url_list:
        if isinstance(u, str) and u:
            return u
    return None
