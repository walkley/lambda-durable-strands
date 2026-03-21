# Strands Agents + Lambda Durable Functions

[中文文档](README.zh-CN.md)

Run [Strands](https://strandsagents.com/) AI agents on AWS Lambda with
[Durable Functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
for fault-tolerant, long-running execution.

> **Note:** Lambda Durable Functions is currently available in limited regions.
> Check [regional availability](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
> before deploying.

## How It Works

### The Problem

A Strands agent's event loop — LLM call → tool execution → LLM call → ... —
runs as a single, continuous process. Lambda Durable Functions can checkpoint
and resume a function, but only at explicit `ctx.step()` / `ctx.invoke()`
boundaries. There is no way to inject a durable checkpoint into the middle of
the Strands event loop from the outside.

### Why Two Lambdas

The Durable Functions SDK supports both JavaScript and Python. This project
uses a Node.js orchestrator + Python agent step to demonstrate that Lambdas
in different languages can work together within Durable Functions.

So the architecture splits into:

- **Orchestrator** (Node.js, Durable Function) — owns the durable loop, calls
  `ctx.invoke()` to run the agent step Lambda on each iteration. Each invoke is
  automatically checkpointed by the durable runtime.
- **Agent Step** (Python, regular Lambda) — runs the Strands agent for exactly
  one step (one LLM call + one tool execution), then returns control to the
  orchestrator.

This turns the agent's continuous event loop into a series of discrete,
durable steps.

### Why a Subprocess + `os._exit()`

The Strands agent event loop is not designed to pause mid-execution. Once
`agent(prompt)` is called, it runs until the LLM returns `end_turn`. There is
no built-in "pause after this tool call and return" API.

To force a pause, the Agent Step Lambda uses a `CheckpointHook` that listens
for `MessageAddedEvent`. When a `toolUse` or `toolResult` message is detected,
the hook needs to stop the agent immediately. But:

- Raising an exception would be caught by the Strands event loop internals.
- Setting a flag and waiting for a clean exit point doesn't exist in the
  current SDK.
- `os._exit()` terminates the process instantly, bypassing all exception
  handlers and cleanup — exactly what we need.

The problem is that `os._exit()` would kill the Lambda runtime itself,
preventing it from returning a response. So the agent runs in a **subprocess**:

1. The Lambda handler (parent process) spawns a subprocess to run the agent.
2. The subprocess's `CheckpointHook` detects a tool message, writes checkpoint
   metadata to an OS pipe, and calls `os._exit(42)`.
3. The parent process reads the pipe, sees exit code 42, and returns
   `{"status": "checkpoint", ...}` to the orchestrator.
4. On normal completion (exit code 0), the subprocess writes the final result
   to the same pipe before exiting.

```
Orchestrator (durable loop)
  │
  ├─ ctx.invoke(agent step)  ──► LLM returns toolUse  ──► checkpoint (exit 42)
  ├─ ctx.invoke(agent step)  ──► tool runs, toolResult ──► checkpoint (exit 42)
  ├─ ctx.invoke(agent step)  ──► LLM returns toolUse  ──► checkpoint (exit 42)
  ├─ ...
  └─ ctx.invoke(agent step)  ──► LLM returns end_turn ──► done (exit 0)
```

### Session Persistence

Session state (conversation history) is persisted to S3 via
`S3SessionManager`. When the agent step Lambda is invoked again after a
checkpoint, it loads the session from S3 and resumes with `agent(None)` —
the SDK detects existing history and continues from where it left off.

## Project Structure

```
lambda_agent/handler.py               — Agent step Lambda (Python)
lambda_orchestrator/handler.mjs       — Orchestrator Lambda (Node.js)
template.yaml                         — SAM template
deploy.sh                             — Build and deploy
test.sh                               — Invoke and poll for result
cleanup.sh                            — Empty S3 and delete stack
```

## Prerequisites

- AWS CLI configured with credentials
- [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- [Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) model access enabled

## Deploy

```bash
./deploy.sh                    # default stack name: strands-durable-poc
./deploy.sh my-stack              # custom stack name
```

## Test

```bash
./test.sh                                    # default prompt: calculate 123 * 456 + 789
./test.sh my-stack "what time is it"         # custom stack name and prompt
```

The script invokes the orchestrator asynchronously and polls until completion.

## Cleanup

```bash
./cleanup.sh                   # empties S3 bucket and deletes the stack
./cleanup.sh my-stack          # custom stack name
```

## License

[MIT](LICENSE)
