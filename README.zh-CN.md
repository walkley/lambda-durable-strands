# Strands Agents + Lambda durable functions

[English](README.md)

本项目演示如何在 [AWS Lambda durable functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
上运行 [Strands](https://strandsagents.com/) AI Agent，让 agent 具备容错能力，中断后能从
断点继续运行。

## 背景与动机

持久化执行（durable execution）正在成为支撑 AI agent 的一种通用做法。Temporal、
[DBOS](https://www.dbos.dev/)、[Restate](https://restate.dev/)、Inngest、
[LangGraph](https://www.langchain.com/langgraph)、Cloudflare Durable Objects，还有
[AWS Lambda durable functions](https://aws.amazon.com/blogs/aws/build-multi-step-applications-and-ai-workflows-with-aws-lambda-durable-functions)，
针对的都是同一个问题。agent 任务越跑越长，短则几分钟，长则几个小时。中途一旦崩溃，已经
跑出来的进度和花掉的 token 就全作废了，而 human-in-the-loop 还要求能长时间挂起。这些
方案的思路也一致。每次 LLM 调用、每次工具返回之后都记一个 checkpoint，出错时从最近的
checkpoint 幂等重放，不必从头再来。

本项目把这套思路用到 AWS Lambda durable functions 上，重点是 checkpoint 的粒度。AWS
官方示例
（[sample-ai-workflows-in-aws-lambda-durable-functions](https://github.com/aws-samples/sample-ai-workflows-in-aws-lambda-durable-functions)）
里的 "Durable Strands Agent" 把整个 agent 调用塞进一个 durable step。这样很简单，但粒度
太粗。一旦失败就得整体重跑，也绕不过单次调用 15 分钟的上限。相比之下，这里把粒度细到
每次 LLM 调用、每次工具执行各算一步。实现上不改 agent 循环，也不靠 subprocess、
`os._exit()` 这类 hack，而是用 `stream_async` 把循环当成事件流来消费，在消息边界处停下。
这套机制对远端 MCP 工具一样有效。

## 架构

系统分成两个 Lambda。

- **Orchestrator**（Node.js，durable function），持久化循环跑在这里。它每轮调一次
  `ctx.invoke()` 运行 Agent Step，每次 invoke 都由 durable 运行时自动 checkpoint。
- **Agent Step**（Python，普通 Lambda），运行 Strands agent，跑到下一个工具边界就返回。

durable execution SDK 同时支持 JavaScript 和 Python，这里两种都用上，顺带演示两种语言的
Lambda 在同一个 durable execution 里协同工作。

## 工作原理

### 核心难点

Strands agent 的事件循环是 LLM 调用、工具执行、再 LLM 调用这样不断往复的过程。而 durable
functions 只能在 `ctx.step()` / `ctx.invoke()` 这些显式边界上记 checkpoint，没办法在循环
中途插入 checkpoint。

### 暂停机制

`agent.stream_async()` 会把事件循环变成一个异步事件流。Agent Step 不在 hook 里中断 agent，
而是反过来，自己作为消费方拉这个流，拉到第一个工具边界就停下来。所谓“停”，其实就是一句
`break`，既不用子进程，也不用 `os._exit()`、OS pipe 或退出码。

在 `MessageAddedEvent` 上注册一个轻量的 `CheckpointDetector`，记下它碰到的第一个边界。

| 边界 | 触发时机 | 含义 |
|------|---------|------|
| `toolUse` | assistant 消息里出现 `toolUse` 块 | LLM 调用已完成，工具还没执行 |
| `toolResult` | user 消息里出现 `toolResult` 块 | 工具已执行，下一次 LLM 调用还没开始 |

这两个信号都发生在事件循环层面，跟工具定义在哪无关。不管是本地工具还是远端 MCP 工具，
都会触发。

> 这里没有用 SDK 的 interrupt 机制。`BeforeToolCallEvent` interrupt 只能在“工具执行前”
> 这一个点中断，粒度不够。工具级的 interrupt 又得改工具本身的代码，对 MCP 工具根本做不到。

### 执行流程

1. Agent Step 调用 `agent.stream_async(prompt)`，LLM 返回 `toolUse`。检测器记下边界，
   Lambda 退出事件流。
2. Lambda 返回 `{"status": "checkpoint", "checkpoint_type": "toolUse", "tool_name": ...}`。
3. Orchestrator 只带着 session id 再调一次 Agent Step。
4. Lambda 从 S3 读回历史，调用 `agent.stream_async(None)`。工具执行后，循环要么走到下一个
   `toolResult` 边界再停一次，要么直接到 `end_turn` 结束。

```
Orchestrator（持久化循环）
  │
  ├─ ctx.invoke(agent step, prompt)  ──► LLM 返回 toolUse    ──► break → checkpoint (toolUse)
  ├─ ctx.invoke(agent step, session) ──► 工具执行, toolResult ──► break → checkpoint (toolResult)
  ├─ ctx.invoke(agent step, session) ──► LLM 返回 toolUse    ──► break → checkpoint (toolUse)
  ├─ ...
  └─ ctx.invoke(agent step, session) ──► LLM 返回 end_turn   ──► 完成
```

循环在 `toolUse` 和 `toolResult` 两处都会断开，于是每次 LLM 调用、每次工具执行都单独
成为一个 durable step。下图是 prompt “calculate 123 * 456 + 789” 的真实执行。durable
运行时记录了三步，分别是返回 `toolUse` 的 LLM 调用、calculator 工具执行，以及最后生成
答案的 LLM 调用。

![Durable execution 运行步骤](lambda_durable_strands_events.png)

### 会话持久化

会话状态（对话历史）由 `S3SessionManager` 负责持久化。每新增一条消息（`MessageAddedEvent`），
就立刻写一次 S3。这次写入发生在对应事件传给消费方之前，所以不管在哪个边界停下，那条消息都
已经在 S3 里了。下次 Lambda 被调用时，先从 S3 把历史读回来，再用 `agent.stream_async(None)`
接着跑。工具只在恢复的那一步执行一次，不会重复。

> `S3SessionManager` 只是其中一种选择。SDK 还内置了 `FileSessionManager`，也支持自定义
> 实现。详见 [Session Management](https://strandsagents.com/docs/user-guide/concepts/agents/session-management/)。

### 原生 checkpoint 支持（进行中）

SDK 正在做一套面向这个场景的原生 durable execution 支持，包括实验性的
`strands.experimental.checkpoint` 模块和一个 `"checkpoint"` 停止原因，追踪在
[strands-agents/sdk-python#1369](https://github.com/strands-agents/sdk-python/issues/1369)。
它的设计和本项目手搓的这套基本一致。

- cycle 的两个边界 `after_model` 和 `after_tools`，就是本项目的 `toolUse` 和 `toolResult`。
- `Checkpoint` 只是个边界标记，会话状态交给 `SessionManager`。这和本项目用
  `S3SessionManager` 存状态是同一种分工。
- 恢复方式跟 interrupt 一样，用 `checkpointResume` 内容块加 `stop_reason="checkpoint"`。
- 每工具粒度交给自定义 `ToolExecutor`。

到最新的 1.42.0 为止，只有数据类型和 `"checkpoint"` 这个停止原因，暂停和恢复还没接进
event loop，所以本项目暂时用 `stream_async` 驱动。等原生 API 落地，agent step 可以直接
换过去，编排器和这套双边界模型都不用改。

## 项目结构

```
lambda_agent/handler.py               — Agent Step Lambda (Python)
lambda_orchestrator/handler.mjs       — Orchestrator Lambda (Node.js)
template.yaml                         — SAM 模板
deploy.sh                             — 构建并部署
test.sh                               — 调用并轮询结果
cleanup.sh                            — 清空 S3 并删除 stack
```

## 前置条件

- AWS CLI 已配置凭证
- [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- [Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) 模型访问权限已开通

## 部署

```bash
./deploy.sh                    # 默认 stack 名：strands-durable-poc
./deploy.sh my-stack           # 自定义 stack 名
```

## 测试

```bash
./test.sh                                      # 默认 prompt：calculate 123 * 456 + 789
./test.sh my-stack "what time is it"           # 自定义 stack 名和 prompt
```

脚本会异步调用 Orchestrator，然后轮询状态，直到执行结束。

## 清理

```bash
./cleanup.sh                   # 清空 S3 bucket 并删除 stack
./cleanup.sh my-stack          # 自定义 stack 名
```

## 许可证

[MIT](LICENSE)
