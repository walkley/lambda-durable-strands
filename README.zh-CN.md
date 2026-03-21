# Strands Agents + Lambda Durable Functions

[English](README.md)

在 AWS Lambda 上运行 [Strands](https://strandsagents.com/) AI Agent，借助
[Durable Functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
实现容错、可中断的长时间执行。

> **注意：** Lambda Durable Functions 目前仅在部分区域可用，部署前请查看
> [区域可用性](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)。

## 工作原理

### 问题背景

Strands agent 的事件循环 — LLM 调用 → 工具执行 → LLM 调用 → ... — 是一个连续运行的
过程。Lambda Durable Functions 可以对函数做 checkpoint 并恢复，但只能在显式的
`ctx.step()` / `ctx.invoke()` 边界处进行。没有办法从外部向 Strands 事件循环的中间
注入一个持久化 checkpoint。

### 为什么需要两个 Lambda

Durable Functions SDK 支持 JavaScript 和 Python。这里选择 Node.js 编排器 +
Python agent 步骤，是为了展示不同语言的 Lambda 可以在 Durable Functions 中协同工作。

因此架构拆分为：

- **Orchestrator**（Node.js，Durable Function）— 拥有持久化循环，每次迭代通过
  `ctx.invoke()` 调用 Agent Step Lambda。每次 invoke 都会被 durable 运行时自动
  checkpoint。
- **Agent Step**（Python，普通 Lambda）— 运行 Strands agent 恰好一步（一次 LLM
  调用 + 一次工具执行），然后将控制权交还给编排器。

这样就把 agent 的连续事件循环拆成了一系列离散的、可持久化的步骤。

### 为什么用子进程 + `os._exit()`

Strands agent 的事件循环不支持中途暂停。一旦调用 `agent(prompt)`，它会一直运行到
LLM 返回 `end_turn`。SDK 没有内置的"执行完这个工具调用就暂停并返回"的 API。

为了强制暂停，Agent Step Lambda 使用 `CheckpointHook` 监听 `MessageAddedEvent`。
当检测到 `toolUse` 或 `toolResult` 消息时，hook 需要立即停止 agent。但是：

- 抛异常会被 Strands 事件循环内部捕获。
- 设置标志位等待干净退出点 — 当前 SDK 中不存在这样的机制。
- `os._exit()` 会立即终止进程，绕过所有异常处理和清理 — 正是我们需要的。

问题在于 `os._exit()` 会杀死 Lambda 运行时本身，导致无法返回响应。所以 agent 运行在
**子进程**中：

1. Lambda handler（父进程）启动子进程运行 agent。
2. 子进程的 `CheckpointHook` 检测到工具消息，通过 OS pipe 写入 checkpoint 元数据，
   然后调用 `os._exit(42)`。
3. 父进程读取 pipe，检测到退出码 42，向编排器返回
   `{"status": "checkpoint", ...}`。
4. 正常完成时（退出码 0），子进程在退出前将最终结果写入同一个 pipe。

```
Orchestrator（持久化循环）
  │
  ├─ ctx.invoke(agent step)  ──► LLM 返回 toolUse    ──► checkpoint (exit 42)
  ├─ ctx.invoke(agent step)  ──► 工具执行, toolResult ──► checkpoint (exit 42)
  ├─ ctx.invoke(agent step)  ──► LLM 返回 toolUse    ──► checkpoint (exit 42)
  ├─ ...
  └─ ctx.invoke(agent step)  ──► LLM 返回 end_turn   ──► 完成 (exit 0)
```

下图是 Lambda 控制台中 prompt "calculate 123 * 456 + 789" 的实际运行截图。
Durable 运行时记录了三个操作 — 第一次 LLM 调用返回 `toolUse`、calculator
工具执行、以及最终 LLM 调用生成答案：

![Durable execution 运行步骤](lambda_durable_strands_events.png)

### 会话持久化

会话状态（对话历史）通过 `S3SessionManager` 持久化到 S3。当 Agent Step Lambda 在
checkpoint 后再次被调用时，它从 S3 加载会话并以 `agent(None)` 恢复 — SDK 检测到
已有历史记录后会从上次中断的地方继续执行。

> 这里使用 `S3SessionManager` 仅作为示例。Strands Agents SDK 还内置了
> `FileSessionManager`，也支持第三方 session manager 实现，可根据实际需求替换。
> 详见 [Session Management](https://strandsagents.com/docs/user-guide/concepts/agents/session-management/)。

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
- 已安装 [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- 已开通 [Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) 模型访问权限

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

脚本会异步调用 orchestrator 并轮询直到执行完成。

## 清理

```bash
./cleanup.sh                   # 清空 S3 bucket 并删除 stack
./cleanup.sh my-stack          # 自定义 stack 名
```

## 许可证

[MIT](LICENSE)
