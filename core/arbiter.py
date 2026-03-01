"""
EmojiLikeArbiter 简化版 -> 响应提示器

功能变更：
不再进行多Bot仲裁，仅用于在解析开始前给消息贴表情，
作为“已收到/正在处理”的视觉反馈。
"""

from typing import Any

class EmojiLikeArbiter:
    """
    基于 CQHTTP 表情点赞的响应提示器
    """

    # 使用表情 ID 124 (通常显示为爱心或勾，视客户端而定，作为确认收到信号)
    _EMOJI_ID = 124
    _EMOJI_TYPE = "1"

    async def notify(self, bot: Any, message_id: int) -> None:
        """
        执行贴表情操作
        
        :param bot: Bot 实例
        :param message_id: 消息 ID
        """
        try:
            await bot.set_msg_emoji_like(
                message_id=message_id,
                emoji_id=self._EMOJI_ID,
                emoji_type=self._EMOJI_TYPE,
                set=True,
            )
        except Exception:
            # 接口调用失败（如不支持或网络问题）直接忽略，不影响解析流程
            pass
