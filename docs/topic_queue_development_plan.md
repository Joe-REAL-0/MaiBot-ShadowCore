# MaiBot “多话题队列”机制开发规划 (Topic Queue Design)

## 1. 需求与目标
在群聊环境中，用户经常会并行讨论多个不同的话题（例如：A 和 B 在聊游戏，C 和 D 在聊工作）。当前 MaiBot 的 `MaisakaHeartFlowChatting` 采用线性 `_chat_history`，这会导致不同话题的上下文互相干扰，降低 Planner 和 Timing Gate 的决策准确性。
**目标**：引入“话题队列（Topic Queue）”机制，使 Bot 能够：
1. 自动识别并拆分群聊中的并行话题。
2. 为不同的话题单独维护历史上下文。
3. Timing Gate 可以独立评估每个话题的参与时机。
4. 主 Planner 仅基于特定话题的干净上下文进行回复。

---

## 2. 架构改造点

### 2.1 引入话题管理器 (Topic Manager)
弃用原有的单一 `list` 形式的 `_chat_history`，引入新的数据结构。

**数据结构设计建议 (`src/maisaka/types.py` 或新建文件)**：
```python
from pydantic import BaseModel, Field
from typing import List
from datetime import datetime

class TopicQueue(BaseModel):
    topic_id: str
    summary: str = "新话题"       # 话题摘要，用于给 Timing Gate 参考
    messages: List[Any] = []     # 该话题下的上下文消息 (LLMContextMessage)
    last_active: datetime = Field(default_factory=datetime.now)
    is_active: bool = True       # 超过一定时间无新消息则归档
```

### 2.2 消息预处理与分类器 (Topic Classifier)
在 `src/maisaka/reasoning_engine.py` 的 `_ingest_messages` 阶段加入话题分类。
由于每条消息都过 LLM 成本太高且延迟大，建议采用**微型 LLM 任务 (Sub Agent)** 或 **本地轻量级模型 / 向量检索** 的方式。
- **流程**：收到 `SessionMessage` -> 提取当前活跃的话题摘要 -> 询问 LLM “这条消息属于哪个话题，还是开启了新话题？” -> 将转换后的 `LLMContextMessage` 塞入对应的 `TopicQueue.messages`。

### 2.3 Timing Gate 门控重构
目前的 Timing Gate 只接收最近的 24 条线性消息。重构后，应当让 Timing Gate 纵览全局的话题状态。
- **Prompt 调整**：告诉 Timing Gate 当前有 N 个活跃话题，并附上每个话题的 `summary` 以及最新的 2-3 条消息。
- **工具定义调整**：`continue` 工具需要增加一个必填参数 `target_topic_id`，让模型明确指出它打算参与哪个话题。

### 2.4 Planner 上下文隔离
在 `src/maisaka/chat_loop_service.py` 中，当 Planner 被触发时，它将从 Timing Gate 接收到 `target_topic_id`。
`select_llm_context_messages` 函数需要修改，仅从对应的 `TopicQueue` 中提取上下文，从而保证大模型不会被群里的其他闲聊干扰。

---

## 3. 分步实施计划

### 阶段一：数据结构扩充与兜底机制 (Fallback)
1. **目标**：在引入多队列的同时，保留一条全局线性历史作为兜底，防止零碎散乱的消息丢失上下文。
2. **动作**：
   - **保留全局历史并新增队列字典**：在 `MaisakaHeartFlowChatting` 中，不要删除原有的 `self._chat_history`（将其作为全局的时间轴记录），而是**新增** `self.topic_queues: dict[str, TopicQueue]`。
   - **消息全局入库**：所有新消息在经过预处理后，无论属于哪个话题，依然会先按时间戳顺序追加到全局的 `self._chat_history` 中。
   - **设计兜底逻辑**：定义一个特殊的标识符（如 `FALLBACK`）。如果后续分类器认为某条消息“既不属于任何现存活跃话题队列，又不足以单独开辟一个新话题”，则放弃放入具体的 `TopicQueue`。
   - **兜底回退行为**：在触发 `FALLBACK` 时，或者在第一阶段过渡期，完全沿用当前代码已有的旧逻辑——即直接从全局的 `self._chat_history` 截取最新的 `max_context_size` 条消息，交给模型去判定是否回复。

### 阶段二：实现 Topic Classifier 子代理与提示词构建
1. **目标**：实现新消息的话题自动分发，精准分类群聊内容。
2. **动作**：
   - **设计 `Prompt` 模板 (`maisaka_topic_classifier`)**：构建一个专门用于分类的系统提示词。提示词需要包含三部分核心信息：
     1. **当前活跃话题列表**：以列表形式展示当前存在的话题队列，包括 `topic_id` 和摘要（例如：`[Topic_A] 讨论原神抽卡`）。
     2. **分类规则说明**：明确告知模型它的唯一任务是“将新消息归类到上述话题中，或者判定为全新话题”。如果属于现有话题，返回对应的 `topic_id`；如果完全不相干，返回特定标记（如 `NEW_TOPIC`）。
     3. **待分类消息批次**：由于每次 API 调用的耗时，建议增加一个缓冲池，积攒 2-3 秒的消息后，作为一个 Batch 统一交给模型进行分类，减少网络往返开销。
   - **格式化输出限制**：为了保证代码能稳定解析结果，必须要求模型以严格的 JSON 格式输出（例如：`{"message_id_1": "Topic_A", "message_id_2": "NEW_TOPIC"}`）。可以通过 `response_format` 强制约定 JSON，并设置极小的 `max_tokens`。
   - **逻辑集成**：在 `MaisakaReasoningEngine` 中增加异步方法 `_classify_message_topic`，调用 `run_sub_agent` 执行分类，并将结果解析后，把每条消息压入对应 `TopicQueue` 的 `messages` 列表中。每当有消息被压入，都需要更新该话题队列的概要，避免话题队列的概要与实际内容产生偏差。

### 阶段三：双层门控（兴趣筛选 + 时机判断）与 Planner 改造
1. **目标**：通过两步走策略，先基于兴趣选择话题，再判断回复时机，以降低 Token 消耗并提高判断准确性。
2. **动作**：
   - **新增 Topic Selection Gate (话题兴趣筛选)**：
     引入一个前置的子代理（Sub Agent）。该代理的 Prompt 仅包含各个活跃话题的“摘要 (Summary)”以及 `config` 中设定的“Bot兴趣列表”和“性格设定”。
     让大模型评估并挑选出它当前最想参与的 `topic_id`（如果不感兴趣，可直接终止流程）。
   - **改造 Timing Gate (时机门控)**：
     在选定 `topic_id` 后，启动原有的 `Timing Gate`。此时只将选中话题的**详细上下文**交给它，让模型判断当前是否适合插话（输出 `wait`、`no_reply` 或 `continue`）。
   - **Planner 隔离**：
     若决定 `continue`，则将该 `topic_id` 透传给 `_run_interruptible_planner`，Planner 仅根据这个纯净的话题队列生成回复。

### 阶段四：话题清理与总结机制 (GC & Summarize)
1. **目标**：控制话题碎片化程度，防止上下文无限膨胀和内存泄露。
2. **动作**：
   - **数量上限与淘汰机制 (LRU 策略)**：在 `config` 中新增活跃话题队列的最大数量上限（如 `max_active_topic_queues`）。当话题分类器决定要新建话题，但当前队列数已达上限时，强制选中**最不活跃**（`last_active` 最久远）的话题，触发总结并将其归档淘汰，以防止话题被无限制切分。
   - **配置驱动的存活时间 (TTL)**：在 `config` 中新增允许的最大存在/空闲时间设定（如 `topic_queue_max_idle_minutes`）。增加后台定时任务，若某个 `TopicQueue` 超过设定的时间没有新消息，自动触发 `Summarize`，将其压缩并持久化到长短期记忆 (`src/services/memory_service.py`)，然后从活跃字典中移除。

---

## 4. 潜在风险与解决方案

| 风险 | 描述 | 应对方案 |
| :--- | :--- | :--- |
| **延迟 (Latency)** | 在入站时增加了一次分类大模型调用，可能导致最终回复变慢。 | 1. 使用极小的本地模型进行分类。<br>2. 或者将分类与 Timing Gate 合并（即在 Timing Gate 中不仅判断是否回复，同时完成话题切分）。 |
| **上下文碎片化** | 话题切分过细，导致大模型认为前言不搭后语。 | 为每个单独的 Topic Queue 自动拼贴一段全局的背景摘要（Context Summary），以补足缺失的上帝视角。 |
| **Token 消耗增加** | 群聊热闹时，需要频繁评估话题归属。 | 合并消息进行 Batch 处理；对分类器进行微调以减小输入和输出大小。 |

## 5. 开始编码的建议入口
建议您从 `src/maisaka/chat_loop_service.py` 中的 `select_llm_context_messages` 函数开始下手。
您可以先通过硬编码（Hardcode）伪造两个话题队列，测试一下隔离上下文后 Planner 的回复效果是否更专注。如果效果显著，再去实施前面沉重的话题分类器逻辑。
