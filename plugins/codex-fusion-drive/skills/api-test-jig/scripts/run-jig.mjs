#!/usr/bin/env node
/**
 * A→B→C: start local jig (A), GET /health and /greet (B), assert example bodies (C).
 * Live API calls are forbidden until this receipt is pass.
 */
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";

const here = path.dirname(fileURLToPath(import.meta.url));
const skillRoot = path.join(here, "..");
const port = Number(process.env.FUSION_JIG_PORT || 4010);
const host = process.env.FUSION_JIG_HOST || "127.0.0.1";
const outDir = process.env.FUSION_JIG_OUT
  || path.join(process.cwd(), ".fusion-jig");
const engine = process.env.FUSION_JIG_ENGINE || "msw-style";

function get(pathname) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host, port, path: pathname, timeout: 5000 }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => resolve({ status: res.statusCode, body, headers: res.headers }));
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(new Error("timeout")); });
  });
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

fs.mkdirSync(outDir, { recursive: true });
const server = spawn(process.execPath, [path.join(here, "jig-server.mjs")], {
  env: { ...process.env, FUSION_JIG_ENGINE: engine, FUSION_JIG_PORT: String(port) },
  stdio: ["ignore", "pipe", "pipe"],
});
let started = false;
const boot = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error("jig server did not start")), 20000);
  const onChunk = (chunk) => {
    const text = chunk.toString();
    process.stderr.write(text);
    if (/listening/.test(text)) {
      started = true;
      clearTimeout(timer);
      resolve(text.trim());
    }
  };
  server.stdout.on("data", onChunk);
  server.stderr.on("data", onChunk);
  server.on("exit", (code) => {
    if (!started) {
      clearTimeout(timer);
      reject(new Error(`jig server exited ${code}`));
    }
  });
});

let receipt;
try {
  const listenLine = await boot;
  const health = await get("/health");
  const greet = await get("/greet?name=jig");
  const healthJson = JSON.parse(health.body);
  const greetJson = JSON.parse(greet.body);
  const healthOk = health.status === 200 && healthJson.status === "ok" && healthJson.jig === true;
  const greetOk = greet.status === 200 && greetJson.message === "hello, jig";
  const pass = healthOk && greetOk;
  receipt = {
    claimed_surface: "fusion-drive-api-jig",
    engine: /jig-engine=(\S+)/.exec(listenLine)?.[1] || engine,
    a_start: `http://${host}:${port}`,
    b_observe: {
      health: { status: health.status, body: healthJson },
      greet: { status: greet.status, body: greetJson },
    },
    c_assert: {
      health_status_ok: healthOk,
      greet_hello_jig: greetOk,
    },
    verdict: pass ? "pass" : "FAIL",
    live_api_authorized: pass,
    notes: pass
      ? "Local simulated endpoint passed. Live API calls may proceed after this receipt."
      : "Local simulated endpoint failed. Live API calls are forbidden.",
  };
  const text = JSON.stringify(receipt, null, 2) + "\n";
  const receiptPath = path.join(outDir, "last-pass.json");
  const logPath = path.join(outDir, "network-log.json");
  fs.writeFileSync(receiptPath, text);
  fs.writeFileSync(logPath, JSON.stringify(receipt.b_observe, null, 2) + "\n");
  receipt.receipt_path = receiptPath;
  receipt.receipt_sha256 = sha256(text);
  fs.writeFileSync(path.join(outDir, "last-pass.sha256"), receipt.receipt_sha256 + "\n");
  process.stdout.write(JSON.stringify(receipt, null, 2) + "\n");
  if (!pass) process.exitCode = 1;
} catch (err) {
  receipt = {
    claimed_surface: "fusion-drive-api-jig",
    verdict: "FAIL",
    live_api_authorized: false,
    error: String(err && err.message ? err.message : err),
  };
  fs.writeFileSync(path.join(outDir, "last-pass.json"), JSON.stringify(receipt, null, 2) + "\n");
  process.stderr.write(String(err) + "\n");
  process.exitCode = 1;
} finally {
  server.kill("SIGTERM");
  setTimeout(() => server.kill("SIGKILL"), 500).unref();
}
