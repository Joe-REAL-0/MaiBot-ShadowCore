# MaiBot 自然对话 Agent 架构与原理解析报告

## 1. 核心设计理念

MaiBot（内置核心代号为 **MaiSaka**）的设计目标不仅仅是一个“有用的助手（helpful assistant）”，而是一个**在群聊或私聊中表现得像真实人类的“数字生命”**。
为了实现这个目标，MaiSaka 重点解决了传统 LLM 机器人的以下痛点：
- **僵硬的问答模式**：传统机器人对每条消息都进行回复，或者仅通过“被艾特”触发。MaiSaka 引入了主动判断“何时该说话、何时该闭嘴”的机制。
- **上下文不连贯与反应迟钝**：在群聊中，如果在 LLM 生成回复期间群里又有了新消息，传统机器人往往会基于旧上下文回复。MaiSaka 支持**思考打断机制**，新消息可以打断当前的思考，让其重新评估。
- **人格与记忆**：通过动态系统提示词结合长短期记忆，使得机器人的对话风格不断贴近所设定的“Persona（人格）”并能从历史对话中学习（如群内黑话、表达习惯）。

---

## 2. 核心架构与请求流转

MaiSaka 的对话请求流转可以分为三层：**网关接入层 -> 运行时层 -> 推理引擎层**。

### 2.1 网关接入层 (`src/chat/message_receive/bot.py`)
所有的平台（QQ等）消息由底层适配器收集后，统一通过 `MessageServer` 送入 `chat_bot.message_process`。
1. **Hook 机制拦截**：消息流入前后会触发插件系统的 `Hook`（如 `chat.receive.before_process`），允许第三方插件拦截或修改消息。
2. **命令匹配**：系统会通过 `component_query_service` 尝试将文本匹配为命令（如 `/help`），如果是被拦截的命令，直接由对应命令执行器处理并阻断后续的自然语言对话链。
3. **路由分发**：未能被命令拦截的普通消息将被送入 `HeartFCMessageReceiver`（心流控制器），最终路由给 `MaisakaHeartFlowChatting` 运行时。

### 2.2 会话运行时 (`src/maisaka/runtime.py`)
每个会话（单聊或群聊）都会实例化一个 `MaisakaHeartFlowChatting`。
- **消息缓冲与防抖**：收到的消息先放入缓冲队列 (`message_cache`)。如果在设定的时间窗口或频率阈值内，才会触发一轮实际的思考（`_schedule_message_turn`）。
- **打断标记**：如果在运行时已经处于 `running`（大模型正在生成）的状态下又收到新消息，并且当前处于 `Planner` 阶段，会触发一个异步中断标记 (`_planner_interrupt_flag.set()`)，强制打断当前推理。

### 2.3 推理引擎 (`src/maisaka/reasoning_engine.py`)
这是最核心的 Agent 逻辑所在。它采用了一个**双 Agent 循环机制 (Dual-Agent Loop)**。

每次被触发时，会启动 `_start_cycle()`，循环最多执行 `MAX_INTERNAL_ROUNDS` (默认6次) 内部回合。
每一回合经历以下关键阶段：

#### A. Timing Gate (时机门控)
系统并不直接让主模型生成回复内容。而是先运行一个参数量小、响应快、Context 限制小的**子代理 (Sub Agent)**，即 `Timing Gate`。
- **工作原理**：给模型输入当前的短对话上下文和特定的 System Prompt，强制它只能调用特定的门控工具，输出决定：
  1. `wait`: 当前信息不足，或者别人话没说完，再等等。
  2. `no_reply`: 这个话题不适合我参与，或者我当前不想理会，直接跳过。
  3. `continue`: 现在是好时机，应当进入“思考与回复”的主流程。
- **作用**：极大地降低了模型瞎搭话的频率，并且因为只输出极少的 Token（只调工具），开销极低。如果返回 `wait` 或 `no_reply`，本轮推理直接结束。

#### B. Planner (主规划器 / 思考与行动)
如果门控结果是 `continue`，进入主 Planner。
- **可打断性**：主 Planner 使用 `_run_interruptible_planner` 启动。在与 LLM 交互期间（甚至是流式输出期间），如果收到外部新消息，抛出 `ReqAbortException`。然后直接放弃当前生成的废话，用加入新消息的上下文重新决策。
- **工具调用 (Tool Calling)**：Planner 可以调用各种内置或插件提供的工具，如：
  - 发送消息 (`reply`)
  - 搜索工具 (`tool_search`，解决工具过多导致的 Prompt 太长的问题，支持延迟发现 Deferred Tools)
  - 记忆操作等。
- **连续推理**：Planner 执行完某些工具（比如查询了天气），如果未显式结束回合，会进入下一个 Round 继续思考，直到决定生成最终文本或者没有工具可以调用为止。

---

## 3. 上下文与记忆管理 (`src/maisaka/chat_loop_service.py`)

- **历史截断与筛选**：通过 `select_llm_context_messages`，系统不会把所有对话原封不动传给 LLM。它会隐藏掉过早的 Agent 自己发出的普通废话，但是**保留 Agent 过去的工具调用链 (Tool Calls & Results)**，以保证模型有正确的决策记忆。
- **Persona 注入**：`_build_personality_prompt` 动态读取配置中的人格描述，并结合群聊/私聊的额外提示（Group Chat Prompt）渲染成最终的 System Prompt。

---

## 4. 如何进行二次开发

如果你希望对 MaiBot 进行二次开发，可以从以下几个切入点着手：

### 4.1 编写扩展插件 (最推荐)
MaiBot 有完善的插件生态 (`src/plugin_runtime`)：
- **开发普通指令**：利用 `component_query_service` 注册命令，这在 `bot.py` 的网关层就会被处理。
- **开发新工具 (Tools)**：如果你想让大语言模型拥有新的能力（比如查快递、画图、调用自有 API），你可以注册 `PluginToolProvider` 下的工具，提供 JSON Schema，Planner 会自动感知并在必要时通过 `tool_search` 调用。

### 4.2 拦截和修改对话流 (Hook 机制)
查看 `bot.py` 和 `chat_loop_service.py` 里的 `HookSpec`，你可以监听并拦截：
- `chat.receive.before_process` / `chat.receive.after_process`: 拦截和修改入站消息文本。
- `maisaka.planner.before_request` / `maisaka.planner.after_response`: 在大模型请求发送前动态修改 Prompt、上下文消息；或在大模型返回后强行篡改它的思考与工具调用意图。

### 4.3 核心行为干预
- **修改门控逻辑**：修改 `src/maisaka/reasoning_engine.py` 中的 `_build_timing_gate_fallback_prompt` 或者重写 `Timing Gate` 逻辑，调整机器人的搭话欲望。
- **引入新大模型接口**：修改或增加 `src/llm_models/model_client` 中的 Client 实现。

---

## 总结
MaiBot 抛弃了简单的 `Input -> LLM -> Output` 结构，构建了一个包含**缓冲防抖、门控判定 (Timing Gate)、中断式思考 (Interruptible Planner)、行动循环 (Action Loop)** 的复杂事件驱动架构，这是它能产生“拟人感”和“生命感”的核心技术壁垒。
