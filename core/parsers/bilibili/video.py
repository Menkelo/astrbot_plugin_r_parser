from msgspec import Struct, field


class Owner(Struct):
    mid: int
    name: str
    face: str


class Dimension(Struct):
    width: int
    height: int
    rotate: int


class Page(Struct):
    cid: int
    page: int
    from_: str = field(name="from")
    part: str
    duration: int
    vid: str
    weblink: str
    dimension: Dimension


class PageInfo(Struct):
    index: int
    cid: int
    title: str
    duration: int
    cover: str
    timestamp: int


class VideoInfo(Struct):
    bvid: str
    aid: int  # === 关键修复：添加 aid 字段 ===
    videos: int
    tid: int
    tname: str
    copyright: int
    pic: str
    title: str
    pubdate: int
    ctime: int
    desc: str
    state: int
    duration: int
    owner: Owner
    pages: list[Page]

    def extract_info_with_page(self, page_num: int) -> PageInfo:
        # 边界检查
        if page_num < 1:
            page_num = 1
        if page_num > len(self.pages):
            page_num = 1
        
        idx = page_num - 1
        page = self.pages[idx]
        
        # 如果只有 1P，直接用视频标题；如果是多P，拼接分P标题
        display_title = self.title if len(self.pages) == 1 else f"{self.title} - {page.part}"
        
        return PageInfo(
            index=idx,
            cid=page.cid,
            title=display_title,
            duration=page.duration,
            cover=self.pic,
            timestamp=self.pubdate
        )