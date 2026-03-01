import asyncio
import hashlib
import json
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from astrbot.api import logger

K = TypeVar("K")
V = TypeVar("V")


class LimitedSizeDict(OrderedDict[K, V]):
    def __init__(self, *args, max_size=128, **kwargs):
        self.max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: K, value: V):
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)


async def safe_unlink(path: Path):
    if path.exists():
        try:
            if path.is_dir():
                await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            else:
                await asyncio.to_thread(path.unlink, missing_ok=True)
        except Exception as e:
            logger.warning(f"删除 {path} 失败: {e}")


async def exec_ffmpeg_cmd(cmd: list[str]) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
        return_code = process.returncode
    except FileNotFoundError:
        raise RuntimeError("ffmpeg 未安装或无法找到可执行文件")

    if return_code != 0:
        error_msg = stderr.decode().strip()
        # 忽略非致命警告
        if "Error" in error_msg or "Invalid" in error_msg or "No such file" in error_msg:
             logger.warning(f"ffmpeg 错误: {error_msg}")
             if return_code != 0:
                 raise RuntimeError(f"ffmpeg 执行失败: {error_msg}")


async def merge_av(v_path: Path, a_path: Path, output_path: Path) -> None:
    """
    极速合并音视频：使用流复制 (Stream Copy)
    """
    # === 关键修复：合并前检查文件是否存在 ===
    if not v_path.exists() or v_path.stat().st_size == 0:
        raise FileNotFoundError(f"视频源文件缺失或为空: {v_path}")
    if not a_path.exists() or a_path.stat().st_size == 0:
        raise FileNotFoundError(f"音频源文件缺失或为空: {a_path}")
        
    logger.debug(f"⚡ 极速合并: {v_path.name} + {a_path.name}")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(v_path),
        "-i", str(a_path),
        "-c", "copy",
        "-strict", "experimental",
        str(output_path),
    ]
    await exec_ffmpeg_cmd(cmd)
    
    # 确认合并成功
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg 合并未生成有效文件")
        
    await asyncio.gather(safe_unlink(v_path), safe_unlink(a_path))
    logger.debug(f"✅ 合并完成: {output_path.name} ({fmt_size(output_path)})")


def fmt_size(file_path: Path) -> str:
    try:
        return f"{file_path.stat().st_size / 1024 / 1024:.2f} MB"
    except Exception:
        return "未知大小"


def generate_file_name(url: str, suffix: str | None = None) -> str:
    try:
        # 修复：仅使用路径部分，去除 ?query 参数
        parsed = urlparse(url)
        path = parsed.path
        name = Path(path).name
        
        # 再次防御，防止文件名中包含 url 编码字符
        if "%" in name:
            import urllib.parse
            name = urllib.parse.unquote(name)
            
        if not name or name == "/":
            name = hashlib.md5(url.encode()).hexdigest()[:16]
            
        # 确保有后缀
        if not suffix:
            if "." not in name:
                suffix = ".unknown"
            else:
                suffix = ""
                
        # 移除原有的扩展名（如果我们需要强制指定后缀）
        if suffix and name.endswith(suffix):
            pass # 已经有后缀了
        elif suffix:
            # 简单拼接，避免 .m4s.mp4 这种奇怪组合，虽然不影响 ffmpeg 但不好看
            if name.endswith(".m4s"):
                name = name[:-4]
            name += suffix
            
        return name
    except Exception:
        return f"{hashlib.md5(url.encode()).hexdigest()[:16]}{suffix or ''}"


def ck2dict(cookies_str: str) -> dict[str, str]:
    res = {}
    if not cookies_str: return res
    for cookie in cookies_str.split(";"):
        if "=" in cookie:
            name, value = cookie.strip().split("=", 1)
            res[name] = value
    return res


def extract_json_url(data: dict | str) -> str | None:
    if isinstance(data, str):
        try: data = json.loads(data)
        except Exception: return None
    if not isinstance(data, dict): return None
    
    meta: dict[str, Any] | None = data.get("meta")
    if meta:
        keys_to_check = [
            ("detail_1", "qqdocurl"),
            ("detail_1", "desc"),
            ("news", "jumpUrl"),
            ("music", "jumpUrl"),
            ("music", "musicUrl"),
        ]
        for key1, key2 in keys_to_check:
            val = meta.get(key1, {}).get(key2)
            if val and isinstance(val, str):
                val = val.replace("&amp;", "&")
                if "http" in val:
                    import re
                    if m := re.search(r'https?://[^\s,"]+', val):
                        return m.group(0)
    
    found_url = _recursive_find_xhs_url(data)
    if found_url:
        return found_url.replace("&amp;", "&")
        
    return None

def _recursive_find_xhs_url(obj: Any) -> str | None:
    if isinstance(obj, str):
        if "xhslink.com" in obj or "xiaohongshu.com" in obj:
            import re
            if m := re.search(r'https?://[a-zA-Z0-9./?=&_-]+(?:xhslink\.com|xiaohongshu\.com)[a-zA-Z0-9./?=&_-]*', obj):
                return m.group(0)
        return None
    
    if isinstance(obj, dict):
        for v in obj.values():
            if res := _recursive_find_xhs_url(v):
                return res
    
    if isinstance(obj, list):
        for v in obj:
            if res := _recursive_find_xhs_url(v):
                return res
                
    return None
