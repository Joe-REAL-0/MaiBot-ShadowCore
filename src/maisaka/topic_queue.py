"""话题队列（Topic Queue）数据结构与管理器。

本模块定义了多话题队列的核心数据结构 TopicQueue 和统一管理器 TopicManager，
以及话题分类器输出的解析数据类 TopicClassification / ClassifierResult。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import copy
import json
import uuid

from src.common.logger import get_logger

from .context_messages import LLMContextMessage

logger = get_logger("topic_queue")

# 分类器返回的特殊标记
NEW_TOPIC_MARKER = "NEW_TOPIC"


@dataclass
class TopicQueue:
    """单个话题队列，维护一组按时间排序的上下文消息。

    Attributes:
        topic_id: 话题唯一标识符。
        summary: 话题摘要，用于给 Timing Gate 和 Topic Classifier 参考。
        messages: 该话题下的上下文消息列表（浅拷贝，保证原子性）。
        last_active: 最后一次收到新消息的时间。
        is_active: 是否仍为活跃话题；超过一定时间无新消息后可被归档。
    """

    topic_id: str
    summary: str = "新话题"
    messages: List[LLMContextMessage] = field(default_factory=list)
    last_active: datetime = field(default_factory=datetime.now)
    is_active: bool = True

    @property
    def message_count(self) -> int:
        """返回当前队列中的消息数量。"""
        return len(self.messages)


# ---------------------------------------------------------------------------
# 分类器输出数据类
# ---------------------------------------------------------------------------


@dataclass
class TopicClassification:
    """单条消息的话题分类结果。

    Attributes:
        message_id: 被分类的消息标识符。
        topic_id: 分配到的话题 ID，或 NEW_TOPIC / __FALLBACK__。
        reason: 分类理由（由 LLM 生成，用于调试日志）。
    """

    message_id: str
    topic_id: str
    reason: str = ""


@dataclass
class ClassifierResult:
    """分类器完整输出的解析结果。

    Attributes:
        classifications: 每条消息的分类结果列表。
        new_topics: NEW_TOPIC 消息对应的摘要映射，key 为 message_id。
        updated_summaries: 需要更新摘要的已有话题映射，key 为 topic_id。
    """

    classifications: List[TopicClassification] = field(default_factory=list)
    new_topics: Dict[str, str] = field(default_factory=dict)
    updated_summaries: Dict[str, str] = field(default_factory=dict)


def generate_topic_id() -> str:
    """生成语义无关的话题唯一标识符。

    Returns:
        str: 形如 "tq_xxxxxxxx" 的 8 字符十六进制短码。
    """
    return f"tq_{uuid.uuid4().hex[:8]}"


def parse_classifier_response(raw_text: str) -> ClassifierResult:
    """解析分类器 LLM 返回的 JSON 文本为 ClassifierResult。

    对格式错误或缺失字段进行宽容处理：缺失的字段使用空默认值，
    无法解析 JSON 时返回空结果并记录警告。

    Args:
        raw_text: 分类器返回的原始文本（应为 JSON）。

    Returns:
        ClassifierResult: 解析后的分类结果。
    """
    stripped_text = raw_text.strip()
    if not stripped_text:
        logger.warning("分类器返回为空文本，回退到空结果")
        return ClassifierResult()

    try:
        data = json.loads(stripped_text)
    except json.JSONDecodeError:
        logger.warning(f"分类器返回的文本无法解析为 JSON，回退到空结果: {stripped_text[:200]}")
        return ClassifierResult()

    if not isinstance(data, dict):
        logger.warning(f"分类器返回的 JSON 顶层不是对象: {type(data).__name__}")
        return ClassifierResult()

    # 解析 classifications
    classifications: List[TopicClassification] = []
    raw_classifications = data.get("classifications")
    if isinstance(raw_classifications, dict):
        for message_id, entry in raw_classifications.items():
            if not isinstance(entry, dict):
                continue
            topic_id = str(entry.get("topic_id", "")).strip()
            reason = str(entry.get("reason", "")).strip()
            if not topic_id:
                continue
            classifications.append(TopicClassification(
                message_id=str(message_id).strip(),
                topic_id=topic_id,
                reason=reason,
            ))

    # 解析 new_topics：key 为消息 ID，value 包含 summary
    new_topics: Dict[str, str] = {}
    raw_new_topics = data.get("new_topics")
    if isinstance(raw_new_topics, dict):
        for msg_id, entry in raw_new_topics.items():
            if isinstance(entry, dict):
                summary = str(entry.get("summary", "新话题")).strip() or "新话题"
            elif isinstance(entry, str):
                summary = entry.strip() or "新话题"
            else:
                summary = "新话题"
            new_topics[str(msg_id).strip()] = summary

    # 解析 updated_summaries
    updated_summaries: Dict[str, str] = {}
    raw_updated = data.get("updated_summaries")
    if isinstance(raw_updated, dict):
        for topic_id, summary in raw_updated.items():
            normalized_summary = str(summary).strip()
            if normalized_summary:
                updated_summaries[str(topic_id).strip()] = normalized_summary

    return ClassifierResult(
        classifications=classifications,
        new_topics=new_topics,
        updated_summaries=updated_summaries,
    )


class TopicManager:
    """话题队列统一管理器。

    负责话题的创建、查询、消息分配和停用。
    当 topic_id 为 FALLBACK_TOPIC_ID 时，消息不会被放入任何具体队列——
    这意味着该消息仅存在于全局 _chat_history 中，由原有逻辑处理。
    """

    FALLBACK_TOPIC_ID: str = "__FALLBACK__"

    def __init__(self) -> None:
        self._queues: Dict[str, TopicQueue] = {}

    def create_topic(self, topic_id: str, summary: str = "新话题") -> TopicQueue:
        """创建一个新的话题队列。

        Args:
            topic_id: 话题唯一标识符，不能为 FALLBACK_TOPIC_ID。
            summary: 话题摘要。

        Returns:
            TopicQueue: 新创建的话题队列。

        Raises:
            ValueError: 当 topic_id 为 FALLBACK 标识或已存在时抛出。
        """
        if topic_id == self.FALLBACK_TOPIC_ID:
            raise ValueError(f"不能使用保留标识符 {self.FALLBACK_TOPIC_ID} 创建话题")
        if topic_id in self._queues:
            raise ValueError(f"话题 {topic_id} 已存在")

        queue = TopicQueue(
            topic_id=topic_id,
            summary=summary,
            last_active=datetime.now(),
        )
        self._queues[topic_id] = queue
        logger.debug(f"已创建话题队列: topic_id={topic_id}, summary={summary}")
        return queue

    def get_topic(self, topic_id: str) -> Optional[TopicQueue]:
        """获取指定话题队列。

        Args:
            topic_id: 话题唯一标识符。

        Returns:
            Optional[TopicQueue]: 话题队列，不存在时返回 None。
        """
        return self._queues.get(topic_id)

    def assign_message(
        self,
        topic_id: str,
        message: LLMContextMessage,
        *,
        max_messages_per_queue: Optional[int] = None,
    ) -> None:
        """将一条消息分配到指定话题队列。

        消息会被浅拷贝后存入队列，保证话题队列中消息的原子性——
        全局历史的后续修改不会影响话题队列中的消息快照。

        当 topic_id 为 FALLBACK_TOPIC_ID 时，不执行任何操作（消息仅保留在全局历史中）。
        当 max_messages_per_queue 不为 None 且队列消息数超过上限时，滚动丢弃最旧的消息。

        Args:
            topic_id: 目标话题标识符，或 FALLBACK_TOPIC_ID。
            message: 待分配的上下文消息。
            max_messages_per_queue: 单个队列最大消息数量，超出时滚动丢弃最旧消息。
        """
        if topic_id == self.FALLBACK_TOPIC_ID:
            # FALLBACK 路径：不放入任何具体队列
            return

        queue = self._queues.get(topic_id)
        if queue is None:
            logger.warning(f"尝试向不存在的话题队列分配消息: topic_id={topic_id}")
            return

        # 浅拷贝消息，保证原子性
        message_copy = copy.copy(message)
        queue.messages.append(message_copy)
        queue.last_active = datetime.now()

        # 消息数上限保护：超出时滚动丢弃最旧消息
        if max_messages_per_queue is not None and max_messages_per_queue > 0:
            overflow = len(queue.messages) - max_messages_per_queue
            if overflow > 0:
                queue.messages = queue.messages[overflow:]
                logger.debug(
                    f"话题 {topic_id} 消息数超过上限 {max_messages_per_queue}，"
                    f"已丢弃最旧的 {overflow} 条消息"
                )

    def get_active_topics(self) -> List[TopicQueue]:
        """获取所有活跃的话题队列。

        Returns:
            List[TopicQueue]: 活跃的话题队列列表，按最后活跃时间降序排列。
        """
        active_queues = [
            queue for queue in self._queues.values() if queue.is_active
        ]
        active_queues.sort(key=lambda q: q.last_active, reverse=True)
        return active_queues

    def get_topic_messages(self, topic_id: str) -> List[LLMContextMessage]:
        """获取指定话题队列中的所有消息。

        Args:
            topic_id: 话题唯一标识符。

        Returns:
            List[LLMContextMessage]: 该话题的消息列表，话题不存在时返回空列表。
        """
        queue = self._queues.get(topic_id)
        if queue is None:
            return []
        return list(queue.messages)

    def deactivate_topic(self, topic_id: str) -> None:
        """将指定话题标记为不活跃（归档）。

        Args:
            topic_id: 话题唯一标识符。
        """
        queue = self._queues.get(topic_id)
        if queue is None:
            logger.warning(f"尝试停用不存在的话题: topic_id={topic_id}")
            return

        queue.is_active = False
        logger.debug(f"已停用话题队列: topic_id={topic_id}")

    def remove_topic(self, topic_id: str) -> Optional[TopicQueue]:
        """从管理器中彻底移除指定话题队列。

        通常在话题归档（总结 + 持久化到长期记忆）后调用，释放内存。

        Args:
            topic_id: 话题唯一标识符。

        Returns:
            Optional[TopicQueue]: 被移除的话题队列实例，不存在时返回 None。
        """
        queue = self._queues.pop(topic_id, None)
        if queue is None:
            logger.warning(f"尝试移除不存在的话题: topic_id={topic_id}")
        else:
            logger.debug(
                f"已移除话题队列: topic_id={topic_id}, "
                f"summary={queue.summary!r}, 消息数={queue.message_count}"
            )
        return queue

    def get_least_active_topic(self) -> Optional[TopicQueue]:
        """获取最不活跃（last_active 最久远）的活跃话题。

        用于 LRU 淘汰策略：当活跃话题数达到上限时选择要归档的话题。

        Returns:
            Optional[TopicQueue]: 最不活跃的话题，没有活跃话题时返回 None。
        """
        active_queues = [q for q in self._queues.values() if q.is_active]
        if not active_queues:
            return None
        return min(active_queues, key=lambda q: q.last_active)

    def get_expired_topics(self, max_idle_minutes: int) -> List[TopicQueue]:
        """获取超过空闲时限的活跃话题列表。

        Args:
            max_idle_minutes: 最大空闲时间（分钟）。

        Returns:
            List[TopicQueue]: 超过空闲时限的活跃话题列表，按 last_active 升序排列。
        """
        now = datetime.now()
        expired: List[TopicQueue] = []
        for queue in self._queues.values():
            if not queue.is_active:
                continue
            idle_seconds = (now - queue.last_active).total_seconds()
            if idle_seconds >= max_idle_minutes * 60:
                expired.append(queue)
        expired.sort(key=lambda q: q.last_active)
        return expired

    def active_topic_count(self) -> int:
        """返回当前活跃话题的数量。"""
        return sum(1 for q in self._queues.values() if q.is_active)

    def update_topic_summary(self, topic_id: str, summary: str) -> None:
        """更新指定话题队列的摘要。

        Args:
            topic_id: 话题唯一标识符。
            summary: 新的话题摘要文本。
        """
        queue = self._queues.get(topic_id)
        if queue is None:
            logger.warning(f"尝试更新不存在的话题摘要: topic_id={topic_id}")
            return

        old_summary = queue.summary
        queue.summary = summary
        logger.debug(
            f"已更新话题摘要: topic_id={topic_id}, "
            f"旧摘要={old_summary!r} -> 新摘要={summary!r}"
        )

    def get_active_topic_summaries_text(self) -> str:
        """生成当前所有活跃话题的摘要文本，供分类器 Prompt 使用。

        格式示例：
            [tq_a1b2c3d4] 讨论原神抽卡 (3条消息)
            [tq_e5f6g7h8] 工作日程安排 (5条消息)

        当没有活跃话题时返回 "（当前没有活跃话题）"。

        Returns:
            str: 格式化后的活跃话题列表文本。
        """
        active_topics = self.get_active_topics()
        if not active_topics:
            return "（当前没有活跃话题）"

        lines: List[str] = []
        for queue in active_topics:
            lines.append(
                f"[{queue.topic_id}] {queue.summary} ({queue.message_count}条消息)"
            )
        return "\n".join(lines)

    def get_all_topic_summaries(self) -> List[Dict[str, Any]]:
        """获取所有话题队列的摘要信息。

        Returns:
            List[Dict[str, Any]]: 话题摘要列表，每个元素包含
                topic_id, summary, message_count, last_active, is_active。
        """
        summaries: List[Dict[str, Any]] = []
        for queue in self._queues.values():
            summaries.append({
                "topic_id": queue.topic_id,
                "summary": queue.summary,
                "message_count": queue.message_count,
                "last_active": queue.last_active.isoformat(timespec="seconds"),
                "is_active": queue.is_active,
            })
        return summaries

    def get_topic_previews_text(self, preview_count: int = 3) -> str:
        """生成每个活跃话题最近几条消息的预览文本，供 Topic Selection Gate 使用。

        格式示例：
            === [tq_a1b2c3d4] 讨论原神抽卡 ===
            - [用户A] 我今天抽了个五星
            - [用户B] 什么角色？

        当没有活跃话题时返回 "（当前没有活跃话题）"。

        Args:
            preview_count: 每个话题展示的最近消息数量，默认 3 条。

        Returns:
            str: 格式化后的话题预览文本。
        """
        active_topics = self.get_active_topics()
        if not active_topics:
            return "（当前没有活跃话题）"

        sections: List[str] = []
        for queue in active_topics:
            header = f"=== [{queue.topic_id}] {queue.summary} ==="
            recent_messages = queue.messages[-preview_count:] if queue.messages else []
            if not recent_messages:
                sections.append(f"{header}\n（暂无消息）")
                continue

            message_lines: List[str] = []
            for msg in recent_messages:
                text = (msg.processed_plain_text or "").strip()
                if not text:
                    text = "[空消息]"
                # 截断过长的消息预览
                if len(text) > 80:
                    text = text[:77] + "..."
                message_lines.append(f"- {text}")
            sections.append(f"{header}\n" + "\n".join(message_lines))

        return "\n\n".join(sections)

    def find_topic_for_message(self, message_id: str) -> Optional[str]:
        """查找包含指定消息 ID 的活跃话题队列。

        用于 @/提及消息时直接定位触发消息所在的话题，
        从而跳过 Topic Selection Gate。

        Args:
            message_id: 待查找的消息标识符。

        Returns:
            Optional[str]: 包含该消息的话题 ID，未找到时返回 None。
        """
        # 空字符串短路：避免无意义的全量遍历
        if not message_id:
            return None

        for queue in self._queues.values():
            if not queue.is_active:
                continue
            for msg in queue.messages:
                if getattr(msg, "message_id", None) == message_id:
                    return queue.topic_id
        return None


@dataclass
class TopicSelectionResult:
    """Topic Selection Gate 的解析结果。

    Attributes:
        topic_id: 选中的话题 ID，为 None 表示不参与任何话题。
        reason: 选择理由（由 LLM 生成，用于调试日志）。
    """

    topic_id: Optional[str]
    reason: str = ""


def parse_topic_selection_response(raw_text: str) -> TopicSelectionResult:
    """解析 Topic Selection Gate 返回的 JSON 文本。

    对格式错误或缺失字段进行宽容处理：
    无法解析 JSON 时返回 topic_id=None 的结果并记录警告。

    Args:
        raw_text: Topic Selection Gate 返回的原始文本（应为 JSON）。

    Returns:
        TopicSelectionResult: 解析后的选择结果。
    """
    stripped_text = raw_text.strip()
    if not stripped_text:
        logger.warning("Topic Selection Gate 返回为空文本，视为不参与任何话题")
        return TopicSelectionResult(topic_id=None)

    try:
        data = json.loads(stripped_text)
    except json.JSONDecodeError:
        logger.warning(
            f"Topic Selection Gate 返回的文本无法解析为 JSON: {stripped_text[:200]}"
        )
        return TopicSelectionResult(topic_id=None)

    if not isinstance(data, dict):
        logger.warning(
            f"Topic Selection Gate 返回的 JSON 顶层不是对象: {type(data).__name__}"
        )
        return TopicSelectionResult(topic_id=None)

    raw_topic_id = data.get("topic_id")
    reason = str(data.get("reason", "")).strip()

    # topic_id 为 null / None / 空字符串均视为不参与
    if raw_topic_id is None or (isinstance(raw_topic_id, str) and not raw_topic_id.strip()):
        return TopicSelectionResult(topic_id=None, reason=reason)

    return TopicSelectionResult(
        topic_id=str(raw_topic_id).strip(),
        reason=reason,
    )
