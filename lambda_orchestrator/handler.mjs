/**
 * Orchestrator Lambda (Durable Function) — loops invoking agent_step until done.
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
