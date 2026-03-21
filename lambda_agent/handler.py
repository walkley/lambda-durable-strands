"""Agent Step Lambda — runs agent in subprocess, os._exit(42) on checkpoint."""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime

from strands import Agent, tool
from strands.hooks import MessageAddedEvent
from strands.models import BedrockModel
from strands.session.s3_session_manager import S3SessionManager

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

SESSION_BUCKET = os.environ.get("SESSION_BUCKET", "")
SESSION_PREFIX = os.environ.get("SESSION_PREFIX", "sessions/")
EXIT_CODE_CHECKPOINT = 42


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


# ── Checkpoint Hook ────────────────────────────────────────────────────

class CheckpointHook:
    """Trigger os._exit() on toolUse/toolResult to create a checkpoint.

    Both checkpoints are driven by MessageAddedEvent:
    - toolUse: assistant message contains toolUse → session persisted → exit
    - toolResult: user message contains toolResult → session persisted → exit

    Checkpoint details are sent back to the parent process via pipe (w_fd).
    """

    def __init__(self, exit_code, w_fd):
        self._exit_code = exit_code
        self._w_fd = w_fd

    def _send_and_exit(self, checkpoint_type, tool_name):
        """Write checkpoint info to pipe, then exit."""
        with os.fdopen(self._w_fd, "w") as f:
            json.dump({"checkpoint_type": checkpoint_type, "tool_name": tool_name}, f)
        os._exit(self._exit_code)

    def on_message_added(self, event: MessageAddedEvent) -> None:
        """Detect toolUse and toolResult messages, trigger checkpoint."""
        msg = event.message
        role = msg.get("role", "")
        content = msg.get("content", [])

        if role == "assistant":
            for block in content:
                if isinstance(block, dict) and "toolUse" in block:
                    name = block["toolUse"].get("name", "unknown")
                    log.info("checkpoint: toolUse detected (%s)", name)
                    self._send_and_exit("toolUse", name)

        if role == "user":
            for block in content:
                if isinstance(block, dict) and "toolResult" in block:
                    # toolResult only contains toolUseId, no tool name
                    # orchestrator uses the tool_name from the previous toolUse
                    log.info("checkpoint: toolResult detected")
                    self._send_and_exit("toolResult", "")


# ── Agent Runner (subprocess entry point) ──────────────────────────────

def _run_agent(session_id, prompt, bucket, prefix, exit_code, w_fd):
    """Run agent in subprocess. os._exit on checkpoint, write result to pipe on completion."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    hook = CheckpointHook(exit_code, w_fd)

    agent = Agent(
        model=BedrockModel(),
        tools=ALL_TOOLS,
        session_manager=S3SessionManager(
            session_id=session_id, bucket=bucket, prefix=prefix,
        ),
        system_prompt="You are a helpful assistant. Use tools when asked. Be concise.",
    )
    agent.hooks.add_callback(MessageAddedEvent, hook.on_message_added)

    has_history = bool(agent.messages)
    effective_prompt = prompt if not has_history else None
    log.info("agent: sid=%s, messages=%d, %s",
             session_id, len(agent.messages), "resume" if has_history else "start")

    result = agent(effective_prompt)

    output = {"response": str(result)[:2000], "messages": len(agent.messages)}
    with os.fdopen(w_fd, "w") as f:
        json.dump(output, f)


# ── Lambda Handler ─────────────────────────────────────────────────────

def handler(event, context):  # noqa: ARG001
    """Lambda entry point. Runs agent in subprocess, checks exit code."""
    log.info("event: %s", json.dumps(event, default=str)[:500])

    session_id = event["session_id"]
    prompt = event.get("prompt")
    bucket = event.get("bucket", SESSION_BUCKET)
    prefix = event.get("prefix", SESSION_PREFIX)

    # pipe: parent reads r_fd, child writes w_fd
    r_fd, w_fd = os.pipe()

    proc = subprocess.run(
        [
            sys.executable, "-c",
            f"from handler import _run_agent; "
            f"_run_agent({session_id!r}, {prompt!r}, {bucket!r}, {prefix!r}, "
            f"{EXIT_CODE_CHECKPOINT}, {w_fd})",
        ],
        timeout=840,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(w_fd,),
        cwd=os.path.dirname(__file__) or ".",
        env={**os.environ, "PYTHONPATH": ":".join(sys.path)},
    )

    os.close(w_fd)  # close write end in parent

    if proc.stdout:
        log.info("subprocess stdout: %s", proc.stdout[-2000:])
    if proc.stderr:
        log.info("subprocess stderr: %s", proc.stderr[-2000:])
    log.info("exit_code=%d", proc.returncode)

    # read subprocess data from pipe
    with os.fdopen(r_fd, "r") as f:
        pipe_data = f.read()

    if proc.returncode == 0:
        output = json.loads(pipe_data) if pipe_data else {}
        return {"status": "done", **output}

    if proc.returncode == EXIT_CODE_CHECKPOINT:
        checkpoint_info = json.loads(pipe_data) if pipe_data else {}
        return {"status": "checkpoint", **checkpoint_info}

    return {"status": "error", "exit_code": proc.returncode}
