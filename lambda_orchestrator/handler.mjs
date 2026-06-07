/**
 * Orchestrator Lambda (Durable Function) — loops invoking agent_step until done.
 *
 * The agent step runs the Strands agent until the next tool boundary (toolUse or
 * toolResult) and returns `{ status: "checkpoint", checkpoint_type, tool_name }`.
 * The orchestrator re-invokes the step with only the session id; the agent restores
 * history from S3 and continues. Each ctx.invoke() is checkpointed by the durable runtime,
 * so every LLM call and every tool execution becomes its own durable step.
 */
import { withDurableExecution } from "@aws/durable-execution-sdk-js";

const {
  SESSION_BUCKET = "",
  SESSION_PREFIX = "sessions/",
  AGENT_STEP_FUNCTION = "",
  MAX_ROUNDS = "20",
} = process.env;

export const handler = withDurableExecution(async (event, ctx) => {
  const sessionId = event.session_id || "default";
  const prompt = event.prompt;

  if (!prompt) {
    return { status: "error", message: "prompt is required" };
  }

  let stepPayload = {
    session_id: sessionId,
    prompt,
    bucket: SESSION_BUCKET,
    prefix: SESSION_PREFIX,
  };

  const maxRounds = parseInt(MAX_ROUNDS, 10);
  let stepName = "llm-call";
  let lastToolName = "";

  for (let round = 1; round <= maxRounds; round++) {
    console.log(`round ${round} (${stepName}) for session ${sessionId}`);

    const result = await ctx.invoke(
      `${stepName}-${round}`,
      AGENT_STEP_FUNCTION,
      stepPayload,
    );

    console.log(`round ${round} result:`, JSON.stringify(result));

    const status = result?.status || "error";

    if (status === "done") {
      console.log(`completed in ${round} rounds`);
      return {
        status: "done",
        rounds: round,
        response: result.response || "",
      };
    }

    if (status === "checkpoint") {
      const cpType = result.checkpoint_type || "unknown";
      const toolName = result.tool_name || lastToolName || "unknown";

      if (cpType === "toolUse") {
        lastToolName = toolName;
        stepName = `tool-${toolName}`;
      } else if (cpType === "toolResult") {
        stepName = `llm-after-${lastToolName || "tool"}`;
      } else {
        stepName = "agent-step";
      }

      // Resume carries only the session; the agent restores history from S3.
      stepPayload = {
        session_id: sessionId,
        bucket: SESSION_BUCKET,
        prefix: SESSION_PREFIX,
      };
      continue;
    }

    console.error(`round ${round} failed:`, result);
    return { status: "error", round, detail: result };
  }

  return { status: "error", message: `exceeded max rounds (${maxRounds})` };
});
