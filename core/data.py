from asyncio import Task
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict


def repr_path_task(path_task: Path | Task[Path]) -> str:
    if isinstance(path_task, Path):
        return f"path={path_task.name}"
    else:
        return f"task={path_task.get_name()}, done={path_task.done()}"


@dataclass(repr=False, slots=True)
class MediaContent:
    path_task: Path | Task[Path]

    async def get_path(self) -> Path:
        if isinstance(self.path_task, Path):
            return self.path_task
        self.path_task = await self.path_task
        return self.path_task

    def __repr__(self) -> str:
        prefix = self.__class__.__name__
        return f"{prefix}({repr_path_task(self.path_task)})"


@dataclass(repr=False, slots=True)
class AudioContent(MediaContent):
    """音频内容"""
    duration: float = 0.0


@dataclass(repr=False, slots=True)
class FileContent(MediaContent):
    """文件内容"""
    name: str | None = None


@dataclass(repr=False, slots=True)
class VideoContent(MediaContent):
    """视频内容"""
    cover: Path | Task[Path] | None = None
    duration: float = 0.0
    
    # === 新增：是否强制作为文件上传 ===
    is_file_upload: bool = False

    async def get_cover_path(self) -> Path | None:
        if self.cover is None:
            return None
        if isinstance(self.cover, Path):
            return self.cover
        self.cover = await self.cover
        return self.cover

    @property
    def display_duration(self) -> str:
        minutes = int(self.duration) // 60
        seconds = int(self.duration) % 60
        return f"时长: {minutes}:{seconds:02d}"

    def __repr__(self) -> str:
        repr = f"VideoContent(path={repr_path_task(self.path_task)}"
        if self.cover is not None:
            repr += f", cover={repr_path_task(self.cover)}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class ImageContent(MediaContent):
    """图片内容"""
    pass


@dataclass(repr=False, slots=True)
class DynamicContent(MediaContent):
    """动态内容"""
    gif_path: Path | None = None


@dataclass(repr=False, slots=True)
class GraphicsContent(MediaContent):
    """图文内容"""
    text: str | None = None
    alt: str | None = None

    def __repr__(self) -> str:
        repr = f"GraphicsContent(path={repr_path_task(self.path_task)}"
        if self.text:
            repr += f", text={self.text}"
        if self.alt:
            repr += f", alt={self.alt}"
        return repr + ")"


@dataclass(slots=True)
class Platform:
    name: str
    display_name: str


@dataclass(repr=False, slots=True)
class Author:
    name: str
    avatar: Path | Task[Path] | None = None
    description: str | None = None

    async def get_avatar_path(self) -> Path | None:
        if self.avatar is None:
            return None
        if isinstance(self.avatar, Path):
            return self.avatar
        self.avatar = await self.avatar
        return self.avatar

    def __repr__(self) -> str:
        repr = f"Author(name={self.name}"
        if self.avatar:
            repr += f", avatar_{repr_path_task(self.avatar)}"
        if self.description:
            repr += f", description={self.description}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class ParseResult:
    platform: Platform
    author: Author | None = None
    title: str | None = None
    text: str | None = None
    timestamp: int | None = None
    url: str | None = None
    
    contents: list[MediaContent] = field(default_factory=list)
    comment_contents: list[MediaContent] = field(default_factory=list)

    extra: dict[str, Any] = field(default_factory=dict)
    repost: "ParseResult | None" = None

    @property
    def header(self) -> str | None:
        header = self.platform.display_name
        if self.author:
            header += f" @{self.author.name}"
        if self.title:
            header += f" | {self.title}"
        return header

    @property
    def display_url(self) -> str | None:
        return f"链接: {self.url}" if self.url else None

    @property
    def repost_display_url(self) -> str | None:
        return f"原帖: {self.repost.url}" if self.repost and self.repost.url else None

    @property
    def extra_info(self) -> str | None:
        return self.extra.get("info")

    @property
    async def cover_path(self) -> Path | None:
        for cont in self.contents:
            if isinstance(cont, VideoContent):
                return await cont.get_cover_path()
        return None

    @property
    def formatted_datetime(self, fmt: str = "%Y-%m-%d %H:%M:%S") -> str | None:
        return (
            datetime.fromtimestamp(self.timestamp).strftime(fmt)
            if self.timestamp is not None
            else None
        )

    def __repr__(self) -> str:
        return (
            f"platform: {self.platform.display_name}, "
            f"timestamp: {self.timestamp}, "
            f"title: {self.title}, "
            f"text: {self.text}, "
            f"url: {self.url}, "
            f"author: {self.author}, "
            f"contents: {self.contents}, "
            f"comments: {self.comment_contents}, "
            f"extra: {self.extra}, "
            f"repost: <<<<<<<{self.repost}>>>>>>"
        )


class ParseResultKwargs(TypedDict, total=False):
    title: str | None
    text: str | None
    contents: list[MediaContent]
    comment_contents: list[MediaContent]
    timestamp: int | None
    url: str | None
    author: Author | None
    extra: dict[str, Any]
    repost: ParseResult | None
