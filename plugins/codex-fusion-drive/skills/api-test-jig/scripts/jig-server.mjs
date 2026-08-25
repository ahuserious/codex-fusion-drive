#!/usr/bin/env node
/**
 * Local API jig. Prefers OpenAPI Prism; falls back to MSW-style HTTP handlers
 * serving the bundled OpenAPI examples. No live upstream. No secrets.
 */
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const specPath = path.join(here, "..", "fixtures", "fusion-jig.openapi.json");
const spec = JSON.parse(fs.readFileSync(specPath, "utf8"));
const port = Number(process.env.FUSION_JIG_PORT || 4010);
const host = process.env.FUSION_JIG_HOST || "127.0.0.1";
const enginePref = (process.env.FUSION_JIG_ENGINE || "auto").toLowerCase();
const recordDir = process.env.FUSION_JIG_RECORD_DIR
  || path.join(process.cwd(), ".fusion-jig", "recordings");

const handlers = {
  "GET /health": () => ({
    status: 200,
    body: spec.paths["/health"].get.responses["200"].content["application/json"].example,
  }),
  "GET /greet": (url) => {
    const name = url.searchParams.get("name") || "jig";
    return { status: 200, body: { message: `hello, ${name}` } };
  },
};

function record(entry) {
  fs.mkdirSync(recordDir, { recursive: true });
  const file = path.join(recordDir, `${Date.now()}-${entry.method}-${entry.path.replace(/\W+/g, "_")}.json`);
  fs.writeFileSync(file, JSON.stringify(entry, null, 2) + "\n");
}

function startMswStyleServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url || "/", `http://${host}:${port}`);
    const key = `${req.method} ${url.pathname}`;
    const handler = handlers[key];
    const started = Date.now();
    if (!handler) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "not_mocked", path: url.pathname }));
      record({ method: req.method, path: url.pathname, status: 404, engine: "msw-style", ms: Date.now() - started });
      return;
    }
    const result = handler(url);
    res.writeHead(result.status, { "content-type": "application/json", "x-fusion-jig": "1" });
    res.end(JSON.stringify(result.body));
    record({
      method: req.method,
      path: url.pathname,
      query: Object.fromEntries(url.searchParams),
      status: result.status,
      body: result.body,
      engine: "msw-style",
      ms: Date.now() - started,
    });
  });
  return new Promise((resolve) => {
    server.listen(port, host, () => {
      process.stdout.write(`jig-engine=msw-style listening http://${host}:${port}\n`);
      resolve(server);
    });
  });
}

function startPrism() {
  return new Promise((resolve, reject) => {
    const child = spawn(
      "npx",
      ["--yes", "@stoplight/prism-cli", "mock", specPath, "-h", host, "-p", String(port)],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error("prism start timeout"));
    }, 45000);
    const onData = (chunk) => {
      const text = chunk.toString();
      stderr += text;
      if (/Prism is listening|listening on/i.test(text)) {
        clearTimeout(timer);
        process.stdout.write(`jig-engine=prism listening http://${host}:${port}\n`);
        resolve(child);
      }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`prism exited ${code}: ${stderr.slice(0, 400)}`));
    });
  });
}

const engine = enginePref;
if (engine === "prism") {
  await startPrism();
} else if (engine === "msw" || engine === "msw-style") {
  await startMswStyleServer();
} else {
  try {
    await startPrism();
  } catch (err) {
    process.stderr.write(`prism unavailable (${err.message}); using msw-style handlers\n`);
    await startMswStyleServer();
  }
}
