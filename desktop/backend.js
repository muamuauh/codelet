"use strict";

const { spawn, execFile } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const path = require("path");

function _tryBindPort(port) {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(port, "127.0.0.1", () => {
      const addr = srv.address();
      const bound = typeof addr === "object" && addr ? addr.port : 0;
      srv.close(() => resolve(bound));
    });
  });
}

async function findFreePort(preferredPort = 18790) {
  // Prefer a stable localhost port so Chromium localStorage (theme, pane widths, etc.)
  // persists seamlessly across restarts; fall back to an ephemeral port if occupied.
  if (preferredPort > 0) {
    try {
      return await _tryBindPort(preferredPort);
    } catch (_) {}
  }
  return _tryBindPort(0);
}

function httpRequest({ host, port, reqPath, method = "GET", headers = {}, timeoutMs = 1500 }) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host, port, path: reqPath, method, headers, timeout: timeoutMs },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          body += chunk;
        });
        res.on("end", () => resolve({ status: res.statusCode || 0, body }));
      }
    );
    req.on("timeout", () => {
      req.destroy(new Error("request timeout"));
    });
    req.on("error", reject);
    req.end();
  });
}

function resolveDevPython() {
  if (process.env.CODELET_PYTHON && fs.existsSync(process.env.CODELET_PYTHON)) {
    return process.env.CODELET_PYTHON;
  }
  const candidates = [
    path.join(os.homedir(), ".conda", "envs", "codelet", "python.exe"),
    path.join(os.homedir(), ".conda", "envs", "codelet", "bin", "python"),
    "C:\\ProgramData\\miniconda3\\envs\\codelet\\python.exe",
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return process.platform === "win32" ? "python.exe" : "python3";
}

class BackendManager {
  constructor({ isPackaged, repoRoot, resourcesPath }) {
    this.isPackaged = Boolean(isPackaged);
    this.repoRoot = repoRoot;
    this.resourcesPath = resourcesPath;
    this.proc = null;
    this.port = 0;
    this.token = "";
    this.logs = [];
    this._stopping = false;
    this._exitCode = null;
  }

  _pushLog(stream, chunk) {
    const lines = String(chunk || "")
      .replace(/\r\n/g, "\n")
      .split("\n")
      .filter((l) => l.length > 0);
    for (const line of lines) {
      this.logs.push(`[${stream}] ${line}`);
      if (this.logs.length > 120) this.logs.shift();
    }
  }

  getRecentLogs(limit = 30) {
    return this.logs.slice(-limit).join("\n");
  }

  _resolveCommand(port, workspace) {
    const commonArgs = ["--host", "127.0.0.1", "--port", String(port), "--no-open"];
    if (workspace && fs.existsSync(workspace)) {
      commonArgs.push("--workspace", workspace);
    }
    if (this.isPackaged) {
      const exeName = process.platform === "win32" ? "codelet-backend.exe" : "codelet-backend";
      const exe = path.join(this.resourcesPath, "backend", "codelet-backend", exeName);
      if (!fs.existsSync(exe)) {
        throw new Error(`Bundled backend executable not found: ${exe}`);
      }
      return {
        exe,
        args: commonArgs,
        cwd: workspace && fs.existsSync(workspace) ? workspace : os.homedir(),
      };
    }
    const py = resolveDevPython();
    return {
      exe: py,
      args: ["-m", "codelet.web", ...commonArgs],
      cwd: this.repoRoot,
    };
  }

  async start({ workspace = "", secretEnv = null, onUnexpectedExit = null } = {}) {
    if (this.proc) {
      return {
        port: this.port,
        token: this.token,
        origin: `http://127.0.0.1:${this.port}`,
        entryUrl: `http://127.0.0.1:${this.port}/?desktop_token=${encodeURIComponent(this.token)}`,
      };
    }

    this._stopping = false;
    this._exitCode = null;
    this.logs = [];
    this.port = await findFreePort();
    this.token = crypto.randomBytes(24).toString("hex");

    const { exe, args, cwd } = this._resolveCommand(this.port, workspace);
    const env = {
      ...process.env,
      ...(secretEnv && typeof secretEnv === "object" ? secretEnv : {}),
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      CODELET_DESKTOP_TOKEN: this.token,
    };

    const child = spawn(exe, args, {
      cwd,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    this.proc = child;

    let spawnError = null;
    child.on("error", (err) => {
      spawnError = err;
      this._pushLog("err", `spawn error: ${err.message}`);
    });
    child.stdout?.on("data", (d) => this._pushLog("out", d));
    child.stderr?.on("data", (d) => this._pushLog("err", d));
    child.on("exit", (code, signal) => {
      this._exitCode = code ?? signal ?? 0;
      const wasStopping = this._stopping;
      this.proc = null;
      if (!wasStopping && typeof onUnexpectedExit === "function") {
        onUnexpectedExit(this._exitCode, this.getRecentLogs());
      }
    });

    const deadline = Date.now() + 40000;
    while (Date.now() < deadline) {
      if (spawnError) {
        throw new Error(`Failed to launch backend (${exe}): ${spawnError.message}`);
      }
      if (this._exitCode !== null) {
        throw new Error(
          `Backend exited prematurely (code=${this._exitCode}).\n${this.getRecentLogs()}`
        );
      }
      try {
        const res = await httpRequest({
          host: "127.0.0.1",
          port: this.port,
          reqPath: "/api/health",
          method: "GET",
          timeoutMs: 1200,
        });
        if (res.status === 200) {
          return {
            port: this.port,
            token: this.token,
            origin: `http://127.0.0.1:${this.port}`,
            entryUrl: `http://127.0.0.1:${this.port}/?desktop_token=${encodeURIComponent(this.token)}`,
          };
        }
      } catch (_) {
        // Still booting
      }
      await new Promise((r) => setTimeout(r, 250));
    }

    await this.stop();
    throw new Error(`Timed out waiting for codelet backend health check.\n${this.getRecentLogs()}`);
  }

  async stop() {
    this._stopping = true;
    const child = this.proc;
    if (!child) return;

    const pid = child.pid;
    const exitPromise = new Promise((resolve) => {
      if (child.exitCode !== null) return resolve();
      child.once("exit", () => resolve());
    });

    // Attempt graceful shutdown so active plugin/MCP subprocesses are disposed cleanly.
    if (this.port && this.token) {
      try {
        await httpRequest({
          host: "127.0.0.1",
          port: this.port,
          reqPath: "/api/shutdown",
          method: "POST",
          headers: { "X-Codelet-Token": this.token },
          timeoutMs: 2000,
        });
      } catch (_) {
        // Ignore; fall through to wait + force kill.
      }
    }

    const exited = await Promise.race([
      exitPromise.then(() => true),
      new Promise((r) => setTimeout(() => r(false), 3000)),
    ]);

    if (!exited && pid) {
      if (process.platform === "win32") {
        await new Promise((resolve) => {
          execFile(
            "taskkill",
            ["/PID", String(pid), "/T", "/F"],
            { windowsHide: true },
            () => resolve()
          );
        });
      } else {
        try {
          child.kill("SIGKILL");
        } catch (_) {}
      }
    }
    this.proc = null;
  }
}

module.exports = { BackendManager, findFreePort };
