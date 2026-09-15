/** Run one generated simulation through the Interface's normal execution boundary. */

import { spawn } from "node:child_process";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const MAX_RESPONSE_BYTES = 1024 * 1024;

type Scalar = string | number | boolean | null;

function runHelper(
  request: { project_path: string; parameters: Record<string, Scalar> },
  signal?: AbortSignal,
): Promise<Record<string, unknown>> {
  const python = process.env.PI_DEVS_PYTHON;
  const runner = process.env.PI_DEVS_RUNNER;
  if (!python || !runner) {
    throw new Error("The prepared DEVS execution helper is not configured.");
  }

  return new Promise((resolve, reject) => {
    const child = spawn(python, [runner], {
      cwd: process.env.PI_DEVS_WORKSPACE_ROOT,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let responseBytes = 0;
    let settled = false;

    const finish = (error?: Error, value?: Record<string, unknown>) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", abort);
      if (error) reject(error);
      else resolve(value ?? {});
    };
    const abort = () => {
      child.kill("SIGTERM");
      finish(new Error("Simulation execution was cancelled."));
    };
    signal?.addEventListener("abort", abort, { once: true });

    child.stdout.on("data", (chunk: Buffer) => {
      responseBytes += chunk.length;
      if (responseBytes > MAX_RESPONSE_BYTES) {
        child.kill("SIGTERM");
        finish(new Error("Simulation helper response exceeded 1 MiB."));
        return;
      }
      stdout.push(chunk);
    });
    child.stderr.on("data", (chunk: Buffer) => {
      responseBytes += chunk.length;
      if (responseBytes > MAX_RESPONSE_BYTES) {
        child.kill("SIGTERM");
        finish(new Error("Simulation helper response exceeded 1 MiB."));
        return;
      }
      stderr.push(chunk);
    });
    child.on("error", (error) => finish(error));
    child.on("close", (code) => {
      if (settled) return;
      const encoded = Buffer.concat(stdout).toString("utf8").trim();
      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(encoded) as Record<string, unknown>;
      } catch {
        const detail = Buffer.concat(stderr).toString("utf8").trim();
        finish(
          new Error(
            `Simulation helper returned invalid JSON (exit ${code}): ${detail.slice(0, 1000)}`,
          ),
        );
        return;
      }
      if (code !== 0 || typeof payload.error === "string") {
        finish(new Error(String(payload.error ?? `Simulation helper exited with code ${code}.`)));
        return;
      }
      finish(undefined, payload);
    });
    child.stdin.end(JSON.stringify(request));
  });
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "devs_run",
    label: "Run DEVS simulation",
    description:
      "Run the generated project through its prepared simulation runtime and return paths to bounded raw output.",
    promptSnippet: "Run a generated DEVS project through its prepared runtime",
    parameters: Type.Object({
      project_path: Type.String({ description: "Canonical project path relative to the workspace" }),
      parameters: Type.Record(
        Type.String(),
        Type.Union([Type.String(), Type.Number(), Type.Boolean(), Type.Null()]),
        { description: "Simulation parameters declared by simulation.json" },
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const result = await runHelper(params, signal);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
        details: result,
      };
    },
  });
}
