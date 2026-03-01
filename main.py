import nest_asyncio
nest_asyncio.apply()

import asyncio
import re
import os
from concurrent.futures import ThreadPoolExecutor
from itertools import chain
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.data import (
    AudioContent,
    Author,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    Platform,
    VideoContent,
    MediaContent
)
from .core.download import Downloader
from .core.exception import (
    DownloadException,
    DownloadLimitException,
    ParseException,
    SizeLimitException,
    ZeroSizeException,
)
from .core.parsers import (
    BaseParser,
    BilibiliParser,
)
from .core.utils import extract_json_url, exec_ffmpeg_cmd


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=2)

        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_r_parser")
        config["data_dir"] = str(self.data_dir)
        self.cache_dir: Path = self.data_dir / "cache_dir"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        config["cache_dir"] = str(self.cache_dir)
        self.config.save_config()

        self.parser_map: dict[str, BaseParser] = {}
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []
        self.downloader = Downloader(config)
        self.arbiter = EmojiLikeArbiter()
        self.cleaner = CacheCleaner(self.context, self.config)

    # region 生命周期

    async def initialize(self):
        self._register_parser()

    async def terminate(self):
        await self.downloader.close()
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        await self.cleaner.stop()
        self._executor.shutdown(wait=False)

    def _register_parser(self):
        all_subclass = BaseParser.get_all_subclass()
        platform_names = []
        for _cls in all_subclass:
            parser = _cls(self.config, self.downloader)
            platform_names.append(parser.platform.display_name)
            for keyword, _ in _cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.info(f"已加载平台: {'、'.join(platform_names)}")

        patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, re.compile(pt) if isinstance(pt, str) else pt)
            for cls in all_subclass
            for kw, pt in cls._key_patterns
        ]
        patterns.sort(key=lambda x: -len(x[0]))
        self.key_pattern_list = patterns

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type.__name__} 的 parser 实例")

    # endregion

    # region 核心逻辑 (Download / Convert / Send)
    async def _download_content(self, cont: MediaContent) -> tuple[MediaContent, Path | None, str | None]:
        try:
            path = await cont.get_path()
            return cont, path, None
        except SizeLimitException as e:
            return cont, None, str(e)
        except (DownloadLimitException, ZeroSizeException):
            return cont, None, None
        except DownloadException as e:
            return cont, None, f"[下载失败: {e}]"
        except Exception as e:
            logger.error(f"下载未知错误: {e}")
            return cont, None, "[下载错误]"

    def _convert_to_seg(self, cont: MediaContent, path: Path) -> BaseMessageComponent | None:
        match cont:
            case ImageContent():
                return Image(str(path))
            case GraphicsContent():
                return Image(str(path))
            case VideoContent() | DynamicContent():
                if hasattr(cont, "is_file_upload") and cont.is_file_upload:
                    return File(name=path.name, file=str(path))
                return Video(str(path))
            case AudioContent():
                return File(name=path.name, file=str(path))
            case FileContent():
                return File(name=path.name, file=str(path))
        return None

    async def _transcode_to_h264(self, input_path: Path) -> Path:
        output_path = input_path.with_name(f"{input_path.stem}_h264.mp4")
        logger.info(f"正在转码视频为 H.264 (极速模式): {input_path.name} -> {output_path.name}")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-c:v", "libx264",
            "-preset", "superfast",
            "-tune", "zerolatency",
            "-crf", "28",
            "-vf", "scale='min(1280,iw)':-2",
            "-maxrate", "1.5M",
            "-bufsize", "3M",
            "-c:a", "aac",
            "-b:a", "128k",
            str(output_path),
        ]
        await exec_ffmpeg_cmd(cmd)
        return output_path

    async def _send_parse_result(self, event: AstrMessageEvent, result: ParseResult):
        show_download_fail_tip = self.config.get("show_download_fail_tip", True)

        node_uin = str(event.get_sender_id())
        node_name = event.get_sender_name() or "R-Parser"

        async def process_main_content():
            if not result.contents:
                return
            tasks = [self._download_content(c) for c in result.contents]
            download_results = await asyncio.gather(*tasks)
            path_map = {id(c): (p, err) for c, p, err in download_results}
            segs = []
            for cont in result.contents:
                path, error = path_map.get(id(cont), (None, None))
                if error:
                    if show_download_fail_tip:
                        segs.append(Plain(f"\n{error}"))
                    continue
                if path:
                    if seg := self._convert_to_seg(cont, path):
                        segs.append(seg)

            if not segs:
                error_msgs = [err for _, _, err in download_results if err]
                if error_msgs and show_download_fail_tip:
                    msg = "\n".join(error_msgs)
                    await event.send(event.plain_result(msg.strip()))
                return

            has_video = any(isinstance(c, (VideoContent, DynamicContent)) for c in result.contents)
            has_video = any(isinstance(c, (VideoContent, DynamicContent)) for c in result.contents)
            if has_video:
                # 多媒体（尤其多视频）优先使用合并转发，避免平台只吞第一个视频
                media_count = sum(1 for s in segs if isinstance(s, (Video, Image, File, Record)))
                if media_count >= 2:
                    try:
                        nodes = Nodes([])
                        for i, seg in enumerate(segs, start=1):
                            # 可选：每个节点加个序号提示
                            # nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[Plain(f"[{i}/{len(segs)}]")]))
                            nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[seg]))
                        await event.send(event.chain_result([nodes]))
                        return
                    except Exception as e:
                        logger.warning(f"合并转发发送失败，降级逐条发送: {e}")

                # 降级：逐条发（防止一次 chain 多视频只发第一个）
                for seg in segs:
                    try:
                        await event.send(event.chain_result([seg]))
                    except Exception as e:
                        err_msg = str(e)
                        if isinstance(seg, Video) and ("rich media" in err_msg or "1200" in err_msg or "Timeout" in err_msg):
                            logger.warning("视频发送失败(编码不兼容/超时)，尝试转码 H.264 重试...")
                            path_str = getattr(seg, "file", None)
                            if path_str:
                                try:
                                    input_path = Path(path_str)
                                    new_path = await self._transcode_to_h264(input_path)
                                    await event.send(event.chain_result([Video(str(new_path))]))
                                    try:
                                        if input_path.exists():
                                            await asyncio.to_thread(input_path.unlink)
                                    except Exception:
                                        pass
                                    continue
                                except Exception:
                                    pass

                        await event.send(event.plain_result(f"⚠️ 媒体发送失败\n🔗 原链接: {result.url or '未知'}"))
            else:
                nodes = Nodes([])
                for seg in segs:
                    nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[seg]))
                if nodes.nodes:
                    await event.send(event.chain_result([nodes]))

        async def process_comment_content():
            if not result.comment_contents:
                return
            tasks = [self._download_content(c) for c in result.comment_contents]
            download_results = await asyncio.gather(*tasks)
            path_map = {id(c): (p, err) for c, p, err in download_results}
            segs = []
            for cont in result.comment_contents:
                path, error = path_map.get(id(cont), (None, None))
                if path:
                    if seg := self._convert_to_seg(cont, path):
                        segs.append(seg)
            if not segs:
                return
            nodes = Nodes([])
            nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[Plain("评论区↓")]))
            for seg in segs:
                nodes.nodes.append(Node(uin=node_uin, name=node_name, content=[seg]))
            await event.send(event.chain_result([nodes]))

        await asyncio.gather(process_main_content(), process_comment_content())
    # endregion

    # region 事件监听

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        text = event.message_str.strip()

        if umo in self.config["disabled_sessions"]:
            return

        if not text:
            chain = event.get_messages()
            if chain:
                seg1 = chain[0]
                if isinstance(seg1, Json):
                    try:
                        text = extract_json_url(seg1.data)
                    except Exception:
                        pass

        if not text:
            return

        prefixes = self.context.get_config().get("command_prefixes", ["/"])
        is_command = any(text.startswith(p) for p in prefixes)

        if is_command:
            return

        self_id = event.get_self_id()
        chain = event.get_messages()
        if chain and isinstance(chain[0], At) and str(chain[0].qq) != self_id:
            return

        # === 改动：支持同一条消息内多个链接 ===
        matches: list[tuple[int, str, re.Match[str]]] = []
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            for m in pat.finditer(text):
                matches.append((m.start(), kw, m))

        if not matches:
            return

        if self.config.get("arbiter", True) and isinstance(event, AiocqhttpMessageEvent):
            if hasattr(event.message_obj, "message_id"):
                asyncio.create_task(
                    self.arbiter.notify(event.bot, event.message_obj.message_id)
                )

        matches.sort(key=lambda x: x[0])
        seen = set()

        for _, keyword, searched in matches:
            dedup_key = (keyword, searched.group(0), searched.start())
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            try:
                parse_res = await self.parser_map[keyword].parse(keyword, searched)
                await self._send_parse_result(event, parse_res)
            except SizeLimitException as e:
                await event.send(event.plain_result(f"⚠️ {e}"))
            except ParseException as e:
                await event.send(event.plain_result(f"⚠️ {e}"))
            except Exception:
                logger.exception("解析过程中发生未知错误")

    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        if umo in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].remove(umo)
            self.config.save_config()
            yield event.plain_result("解析已开启")
        else:
            yield event.plain_result("解析已开启，无需重复开启")

    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        if umo not in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].append(umo)
            self.config.save_config()
            yield event.plain_result("解析已关闭")
        else:
            yield event.plain_result("解析已关闭，无需重复关闭")
