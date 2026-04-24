"""TopicManager 单元测试。

使用轻量级的假消息对象来避免导入整条项目依赖链，
仅测试 TopicManager 和 TopicQueue 自身的逻辑。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from unittest.mock import MagicMock

import sys

# 在导入 topic_queue 之前，对依赖链中的重量级模块进行打桩，
# 避免触发 sqlmodel / Levenshtein 等项目级依赖。
_STUB_MODULES = [
    "src.chat",
    "src.chat.message_receive",
    "src.chat.message_receive.message",
    "src.common",
    "src.common.data_models",
    "src.common.data_models.message_component_data_model",
    "src.common.logger",
    "src.emoji_system",
    "src.emoji_system.emoji_manager",
    "src.llm_models",
    "src.llm_models.payload_content",
    "src.llm_models.payload_content.message",
    "src.llm_models.payload_content.tool_option",
    "PIL",
    "PIL.Image",
]

_original_modules = {}
for _mod_name in _STUB_MODULES:
    if _mod_name not in sys.modules:
        _original_modules[_mod_name] = None
        sys.modules[_mod_name] = MagicMock()
    else:
        _original_modules[_mod_name] = sys.modules[_mod_name]

import pytest  # noqa: E402

from src.maisaka.topic_queue import TopicManager, TopicQueue  # noqa: E402


# ---------- 假消息对象 ----------

@dataclass
class _FakeMessage:
    """用于测试的假 LLMContextMessage 替代品。"""

    content: str
    timestamp: datetime
    role: str = "user"

    @property
    def processed_plain_text(self) -> str:
        return self.content

    @property
    def count_in_context(self) -> bool:
        return True

    @property
    def source(self) -> str:
        return "fake"

    def to_llm_message(self, enable_visual_message: bool = True) -> Optional[object]:
        return None

    def consume_once(self) -> bool:
        return True


def _make_message(content: str = "测试消息") -> _FakeMessage:
    """创建一条测试用的假消息。"""
    return _FakeMessage(content=content, timestamp=datetime.now())


# ---------- 创建话题 ----------

class TestCreateTopic:
    """测试话题创建功能。"""

    def test_create_topic_basic(self) -> None:
        """基本创建话题。"""
        manager = TopicManager()
        queue = manager.create_topic("topic_a", summary="讨论游戏")

        assert isinstance(queue, TopicQueue)
        assert queue.topic_id == "topic_a"
        assert queue.summary == "讨论游戏"
        assert queue.is_active is True
        assert queue.message_count == 0

    def test_create_topic_default_summary(self) -> None:
        """创建话题时使用默认摘要。"""
        manager = TopicManager()
        queue = manager.create_topic("topic_b")

        assert queue.summary == "新话题"

    def test_create_topic_duplicate_raises(self) -> None:
        """重复创建同一话题应抛出异常。"""
        manager = TopicManager()
        manager.create_topic("topic_a")

        with pytest.raises(ValueError, match="已存在"):
            manager.create_topic("topic_a")

    def test_create_topic_with_fallback_id_raises(self) -> None:
        """使用保留的 FALLBACK 标识符创建话题应抛出异常。"""
        manager = TopicManager()

        with pytest.raises(ValueError, match="保留标识符"):
            manager.create_topic(TopicManager.FALLBACK_TOPIC_ID)


# ---------- 获取话题 ----------

class TestGetTopic:
    """测试话题查询功能。"""

    def test_get_existing_topic(self) -> None:
        """获取已存在的话题。"""
        manager = TopicManager()
        manager.create_topic("topic_a", summary="讨论游戏")

        queue = manager.get_topic("topic_a")
        assert queue is not None
        assert queue.topic_id == "topic_a"

    def test_get_nonexistent_topic(self) -> None:
        """获取不存在的话题应返回 None。"""
        manager = TopicManager()

        assert manager.get_topic("nonexistent") is None


# ---------- 消息分配 ----------

class TestAssignMessage:
    """测试消息分配功能。"""

    def test_assign_message_to_topic(self) -> None:
        """分配消息到具体话题队列。"""
        manager = TopicManager()
        manager.create_topic("topic_a")
        msg = _make_message("你好")

        manager.assign_message("topic_a", msg)

        queue = manager.get_topic("topic_a")
        assert queue is not None
        assert queue.message_count == 1
        assert queue.messages[0].content == "你好"

    def test_assign_message_creates_copy(self) -> None:
        """分配到话题队列中的消息应为拷贝，修改原消息不影响队列。"""
        manager = TopicManager()
        manager.create_topic("topic_a")
        msg = _make_message("原始内容")

        manager.assign_message("topic_a", msg)
        # 修改原消息
        msg.content = "已修改"

        queue = manager.get_topic("topic_a")
        assert queue is not None
        # 队列中的消息应保持原始内容
        assert queue.messages[0].content == "原始内容"

    def test_assign_message_fallback_does_nothing(self) -> None:
        """分配到 FALLBACK 路径时不放入任何队列。"""
        manager = TopicManager()
        manager.create_topic("topic_a")
        msg = _make_message("测试")

        manager.assign_message(TopicManager.FALLBACK_TOPIC_ID, msg)

        queue = manager.get_topic("topic_a")
        assert queue is not None
        assert queue.message_count == 0

    def test_assign_message_to_nonexistent_topic(self) -> None:
        """分配消息到不存在的话题时应静默跳过（仅警告日志）。"""
        manager = TopicManager()
        msg = _make_message("测试")

        # 不应抛出异常
        manager.assign_message("nonexistent", msg)

    def test_assign_message_updates_last_active(self) -> None:
        """分配消息后应更新话题的最后活跃时间。"""
        manager = TopicManager()
        queue = manager.create_topic("topic_a")
        original_time = queue.last_active

        msg = _make_message("新消息")
        manager.assign_message("topic_a", msg)

        assert manager.get_topic("topic_a").last_active >= original_time

    def test_assign_multiple_messages(self) -> None:
        """连续分配多条消息到同一话题。"""
        manager = TopicManager()
        manager.create_topic("topic_a")

        for i in range(5):
            manager.assign_message("topic_a", _make_message(f"消息 {i}"))

        queue = manager.get_topic("topic_a")
        assert queue is not None
        assert queue.message_count == 5


# ---------- 活跃话题查询 ----------

class TestGetActiveTopics:
    """测试活跃话题查询功能。"""

    def test_get_active_topics_empty(self) -> None:
        """没有话题时返回空列表。"""
        manager = TopicManager()

        assert manager.get_active_topics() == []

    def test_get_active_topics_filters_inactive(self) -> None:
        """停用的话题不应出现在活跃列表中。"""
        manager = TopicManager()
        manager.create_topic("topic_a")
        manager.create_topic("topic_b")
        manager.deactivate_topic("topic_a")

        active = manager.get_active_topics()
        assert len(active) == 1
        assert active[0].topic_id == "topic_b"

    def test_get_active_topics_sorted_by_last_active(self) -> None:
        """活跃话题应按最后活跃时间降序排列。"""
        manager = TopicManager()
        manager.create_topic("topic_a")
        manager.create_topic("topic_b")
        # 给 topic_a 分配消息使其最后活跃时间更新
        manager.assign_message("topic_a", _make_message("更新"))

        active = manager.get_active_topics()
        assert len(active) == 2
        assert active[0].topic_id == "topic_a"


# ---------- 获取话题消息 ----------

class TestGetTopicMessages:
    """测试获取话题消息功能。"""

    def test_get_topic_messages_returns_copy(self) -> None:
        """返回的消息列表应为独立副本。"""
        manager = TopicManager()
        manager.create_topic("topic_a")
        manager.assign_message("topic_a", _make_message("消息1"))

        messages = manager.get_topic_messages("topic_a")
        messages.clear()  # 清空返回的列表

        # 原始队列中的消息不受影响
        assert manager.get_topic("topic_a").message_count == 1

    def test_get_topic_messages_nonexistent(self) -> None:
        """获取不存在话题的消息应返回空列表。"""
        manager = TopicManager()

        assert manager.get_topic_messages("nonexistent") == []


# ---------- 停用话题 ----------

class TestDeactivateTopic:
    """测试话题停用功能。"""

    def test_deactivate_topic(self) -> None:
        """停用后 is_active 应变为 False。"""
        manager = TopicManager()
        manager.create_topic("topic_a")

        manager.deactivate_topic("topic_a")

        queue = manager.get_topic("topic_a")
        assert queue is not None
        assert queue.is_active is False

    def test_deactivate_nonexistent_topic(self) -> None:
        """停用不存在的话题应静默跳过。"""
        manager = TopicManager()

        # 不应抛出异常
        manager.deactivate_topic("nonexistent")


# ---------- 话题摘要 ----------

class TestGetAllTopicSummaries:
    """测试话题摘要输出功能。"""

    def test_get_all_topic_summaries(self) -> None:
        """获取所有话题的摘要信息。"""
        manager = TopicManager()
        manager.create_topic("topic_a", summary="讨论游戏")
        manager.create_topic("topic_b", summary="聊工作")
        manager.assign_message("topic_a", _make_message("消息1"))
        manager.deactivate_topic("topic_b")

        summaries = manager.get_all_topic_summaries()
        assert len(summaries) == 2

        summary_map = {s["topic_id"]: s for s in summaries}
        assert summary_map["topic_a"]["summary"] == "讨论游戏"
        assert summary_map["topic_a"]["message_count"] == 1
        assert summary_map["topic_a"]["is_active"] is True

        assert summary_map["topic_b"]["summary"] == "聊工作"
        assert summary_map["topic_b"]["message_count"] == 0
        assert summary_map["topic_b"]["is_active"] is False

    def test_get_all_topic_summaries_empty(self) -> None:
        """没有话题时返回空列表。"""
        manager = TopicManager()

        assert manager.get_all_topic_summaries() == []

# ---------- 阶段四：生命周期管理 ----------

class TestLifecycleManagement:
    """测试阶段四引入的生命周期管理功能。"""

    def test_remove_topic(self) -> None:
        """测试彻底移除话题队列。"""
        manager = TopicManager()
        manager.create_topic("topic_rm")
        assert manager.get_topic("topic_rm") is not None
        
        removed = manager.remove_topic("topic_rm")
        assert removed is not None
        assert removed.topic_id == "topic_rm"
        assert manager.get_topic("topic_rm") is None
        
        # 重复移除返回 None
        assert manager.remove_topic("topic_rm") is None

    def test_get_least_active_topic(self) -> None:
        """测试获取最不活跃的活跃话题。"""
        manager = TopicManager()
        assert manager.get_least_active_topic() is None

        manager.create_topic("topic_old")
        manager.create_topic("topic_new")
        
        queue_old = manager.get_topic("topic_old")
        queue_new = manager.get_topic("topic_new")
        assert queue_old is not None and queue_new is not None
        
        # 强制设置 last_active 差异
        from datetime import datetime, timedelta
        now = datetime.now()
        queue_old.last_active = now - timedelta(hours=2)
        queue_new.last_active = now - timedelta(hours=1)
        
        least_active = manager.get_least_active_topic()
        assert least_active is not None
        assert least_active.topic_id == "topic_old"
        
        # 如果 topic_old 被标记为不活跃，则最不活跃的是 topic_new
        manager.deactivate_topic("topic_old")
        least_active2 = manager.get_least_active_topic()
        assert least_active2 is not None
        assert least_active2.topic_id == "topic_new"

    def test_get_expired_topics(self) -> None:
        """测试获取过期话题。"""
        manager = TopicManager()
        manager.create_topic("topic_1")
        manager.create_topic("topic_2")
        
        queue1 = manager.get_topic("topic_1")
        queue2 = manager.get_topic("topic_2")
        assert queue1 is not None and queue2 is not None
        
        from datetime import datetime, timedelta
        now = datetime.now()
        # topic_1 空闲了 40 分钟
        queue1.last_active = now - timedelta(minutes=40)
        # topic_2 空闲了 20 分钟
        queue2.last_active = now - timedelta(minutes=20)
        
        # max_idle_minutes = 30
        expired = manager.get_expired_topics(30)
        assert len(expired) == 1
        assert expired[0].topic_id == "topic_1"

    def test_assign_message_max_limit(self) -> None:
        """测试消息数超过上限时滚动丢弃最旧消息。"""
        manager = TopicManager()
        manager.create_topic("topic_limit")
        
        # 上限设为 3，存入 5 条消息
        for i in range(5):
            msg = _make_message(f"消息{i}")
            manager.assign_message("topic_limit", msg, max_messages_per_queue=3)
            
        queue = manager.get_topic("topic_limit")
        assert queue is not None
        assert queue.message_count == 3
        # 留下的应该是 "消息2", "消息3", "消息4"
        assert queue.messages[0].processed_plain_text == "消息2"
        assert queue.messages[-1].processed_plain_text == "消息4"

    def test_find_topic_for_message_empty_id(self) -> None:
        """测试空字符串 message_id 直接返回 None。"""
        manager = TopicManager()
        manager.create_topic("topic_test")
        msg = _make_message("消息测试")
        msg.message_id = "test_id"
        manager.assign_message("topic_test", msg)
        
        assert manager.find_topic_for_message("") is None
        assert manager.find_topic_for_message(None) is None  # type: ignore
        assert manager.find_topic_for_message("test_id") == "topic_test"

