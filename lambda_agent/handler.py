"""Agent Step Lambda — drives the Strands agent via stream_async, pausing at tool boundaries.

The agent loop is exposed as an async event stream (`agent.stream_async`). The Lambda
consumes that stream and simply stops iterating at the first tool boundary, which hands
control back cleanly — no subprocess, no os._exit. State is persisted to S3 by
S3SessionManager as each message is added, so the next invocation resumes from history
with `stream_async(None)`.

Two boundaries are detected, matching a durable step to each LLM call and each tool run:
- toolUse:    an assistant message containing a toolUse block (stop BEFORE the tool runs)
- toolResult: a user message containing a toolResult block (stop BEFORE the next LLM call)

Both are event-loop level signals, so they fire for any tool — local or a remote MCP
tool whose body the app does not control. To add an MCP server, load its tools alongside
the local ones and run the agent inside the client's context manager; the checkpoint and
resume logic here is unchanged.
"""

import asyncio
import logging
import os
from datetime import datetime

from strands import Agent, tool
from strands.hooks import MessageAddedEvent
from strands.models import BedrockModel
from strands.session.s3_session_manager import S3SessionManager

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

SESSION_BUCKET = os.environ.get("SESSION_BUCKET", "")
SESSION_PREFIX = os.environ.get("SESSION_PREFIX", "sessions/")


# ── Tools ──────────────────────────────────────────────────────────────

@tool
def current_time() -> str:
    """Get the current date and time."""
    return datetime.now().isoformat()


@tool
def calculator(expression: str) -> str:
    """Evaluate a basic math expression (digits and +-*/.() only).

    Args:
        expression: A basic arithmetic expression to evaluate.
    """
    if not all(c in "0123456789+-*/.() " for c in expression):
        return "Error: only basic arithmetic allowed"
    return str(eval(expression))  # noqa: S307


@tool
def shell(command: str) -> str:
    """Run a shell command and return its output.

    Args:
        command: The shell command to execute.
    """
    import subprocess

    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=720, check=False)
    output = r.stdout.strip()
    if r.returncode != 0:
        output += f"\nSTDERR: {r.stderr.strip()}\nEXIT: {r.returncode}"
    return output


@tool
def sleep_seconds(seconds: int) -> str:
    """Sleep for the specified number of seconds, then return.

    Args:
        seconds: Number of seconds to sleep.
    """
    import time
    log.info("sleep_seconds: sleeping %d seconds...", seconds)
    time.sleep(seconds)
    log.info("sleep_seconds: done")
    return f"Slept for {seconds} seconds."


ALL_TOOLS = [current_time, calculator, shell, sleep_seconds]


# ── Checkpoint Detection ───────────────────────────────────────────────

class CheckpointDetector:
    """Record the first tool boundary seen, so the driver knows where to stop.

    Registered on MessageAddedEvent. The session manager is also a MessageAddedEvent
    consumer, so by the time the driver observes the next streamed event the message
    is already written to S3 — the break point is durable.
    """

    def __init__(self):
        self.checkpoint = None

    def on_message_added(self, event: MessageAddedEvent) -> None:
        if self.checkpoint is not None:
            return
        msg = event.message
        role = msg.get("role", "")
        for block in msg.get("content", []):
            if not isinstance(block, dict):
                continue
            if role == "assistant" and "toolUse" in block:
                name = block["toolUse"].get("name", "unknown")
                log.info("checkpoint: toolUse detected (%s)", name)
                self.checkpoint = {"checkpoint_type": "toolUse", "tool_name": name}
                return
            if role == "user" and "toolResult" in block:
                log.info("checkpoint: toolResult detected")
                self.checkpoint = {"checkpoint_type": "toolResult", "tool_name": ""}
                return


# ── Agent Runner ───────────────────────────────────────────────────────

async def _run(session_id, prompt, bucket, prefix):
    """Drive the agent stream until the first tool boundary or normal completion."""
    detector = CheckpointDetector()

    agent = Agent(
        model=BedrockModel(),
        tools=ALL_TOOLS,
        session_manager=S3SessionManager(
            session_id=session_id, bucket=bucket, prefix=prefix,
        ),
        system_prompt="You are a helpful assistant. Use tools when asked. Be concise.",
    )
    agent.hooks.add_callback(MessageAddedEvent, detector.on_message_added)

    has_history = bool(agent.messages)
    effective_prompt = prompt if not has_history else None
    log.info("agent: sid=%s, messages=%d, %s",
             session_id, len(agent.messages), "resume" if has_history else "start")

    final_result = None
    stream = agent.stream_async(effective_prompt)
    try:
        async for event in stream:
            # The boundary message is already persisted; stop before the loop advances.
            if detector.checkpoint is not None:
                break
            if isinstance(event, dict) and "result" in event:
                final_result = event["result"]
    finally:
        await stream.aclose()

    if detector.checkpoint is not None:
        return {"status": "checkpoint", **detector.checkpoint}

    return {
        "status": "done",
        "response": str(final_result)[:2000] if final_result is not None else "",
        "messages": len(agent.messages),
    }


# ── Lambda Handler ─────────────────────────────────────────────────────

def handler(event, context):  # noqa: ARG001
    """Lambda entry point. Runs one agent segment until a tool boundary or completion."""
    log.info("event: %s", str(event)[:500])

    session_id = event["session_id"]
    prompt = event.get("prompt")
    bucket = event.get("bucket", SESSION_BUCKET)
    prefix = event.get("prefix", SESSION_PREFIX)

    return asyncio.run(_run(session_id, prompt, bucket, prefix))
