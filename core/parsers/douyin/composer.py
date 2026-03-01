import asyncio
import hashlib
from pathlib import Path

from ...data import DynamicContent
from ...utils import exec_ffmpeg_cmd, safe_unlink


class DouyinMediaComposer:
    def __init__(self, downloader, config):
        self.downloader = downloader
        self.config = config

    @staticmethod
    def as_bool(v, default=False) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in {"1", "true", "yes", "on"}:
                return True
            if s in {"0", "false", "no", "off", ""}:
                return False
        return default

    def build_unique_dynamic_contents_from_entries(
        self,
        entries: list[tuple[str, str]],
        vid: str,
        ext_headers: dict[str, str],
    ) -> list[DynamicContent]:
        contents: list[DynamicContent] = []
        seen: set[str] = set()

        for i, (k, u) in enumerate(entries, start=1):
            key = (k or u).strip() if (k or u) else ""
            if not key or not u or key in seen:
                continue
            seen.add(key)

            short = hashlib.md5(f"{vid}|{i}|{key}|{u}".encode()).hexdigest()[:10]
            name = f"douyin_{vid}_dyn_{i}_{short}.mp4"
            task = self.downloader.download_video(u, video_name=name, ext_headers=ext_headers)
            contents.append(DynamicContent(task))
        return contents

    async def merge_dynamic_videos_with_bgm(
        self,
        entries: list[tuple[str, str]],
        vid: str,
        bgm_url: str | None,
        ext_headers: dict[str, str],
    ) -> Path:
        # 去重保序
        uniq: list[tuple[str, str]] = []
        seen: set[str] = set()
        for k, u in entries:
            key = (k or u).strip() if (k or u) else ""
            if not key or not u or key in seen:
                continue
            seen.add(key)
            uniq.append((key, u))
        if not uniq:
            raise RuntimeError("没有可合并动态视频")

        cache_dir = Path(self.config.get("cache_dir", "."))
        work_dir = cache_dir / f"douyin_merge_{vid}_{hashlib.md5(str(uniq).encode()).hexdigest()[:8]}"
        work_dir.mkdir(parents=True, exist_ok=True)

        # 下载分段
        tasks = []
        for i, (_, u) in enumerate(uniq, start=1):
            short = hashlib.md5(f"{vid}|seg|{i}|{u}".encode()).hexdigest()[:8]
            tasks.append(self.downloader.download_video(u, video_name=f"seg_{i:03d}_{short}.mp4", ext_headers=ext_headers))
        seg_paths = await asyncio.gather(*tasks)
        seg_paths = [p for p in seg_paths if p.exists() and p.stat().st_size > 0]
        if not seg_paths:
            raise RuntimeError("分段下载失败")

        list_file = work_dir / "concat.txt"
        list_file.write_text("\n".join([f"file '{p.as_posix()}'" for p in seg_paths]), encoding="utf-8")

        no_audio = work_dir / f"douyin_{vid}_merged_noaudio.mp4"
        final = work_dir / f"douyin_{vid}_merged.mp4"

        await exec_ffmpeg_cmd([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            str(no_audio),
        ])

        if bgm_url:
            try:
                bgm = await self.downloader.download_audio(bgm_url, ext_headers=ext_headers)
                await exec_ffmpeg_cmd([
                    "ffmpeg", "-y",
                    "-i", str(no_audio),
                    "-stream_loop", "-1", "-i", str(bgm),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest",
                    str(final),
                ])
                await safe_unlink(bgm)
                await safe_unlink(no_audio)
            except Exception:
                await safe_unlink(final)
                no_audio.rename(final)
        else:
            await safe_unlink(final)
            no_audio.rename(final)

        await safe_unlink(list_file)
        for p in seg_paths:
            await safe_unlink(p)

        if not final.exists() or final.stat().st_size == 0:
            raise RuntimeError("合并结果无效")
        return final
