"use strict";

const fs = require("fs");
const path = require("path");
const { safeStorage } = require("electron");

const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * OS-encrypted secret vault backed by Electron's `safeStorage`
 * (Windows DPAPI CryptProtectData / macOS Keychain / Linux libsecret).
 *
 * Ciphertext is stored as base64 in `<userData>/secrets.dpapi.json`.
 * Plaintext secrets are NEVER returned over IPC; they are only decrypted inside
 * the main process to inject into the local Python backend child env.
 */
class SecretVault {
  constructor(userDataDir) {
    this.filePath = path.join(userDataDir, "secrets.dpapi.json");
  }

  isAvailable() {
    try {
      return Boolean(safeStorage && safeStorage.isEncryptionAvailable());
    } catch (_) {
      return false;
    }
  }

  _readRaw() {
    try {
      const text = fs.readFileSync(this.filePath, "utf8");
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object" && parsed.entries && typeof parsed.entries === "object") {
        return parsed.entries;
      }
    } catch (_) {}
    return {};
  }

  _writeRaw(entries) {
    const dir = path.dirname(this.filePath);
    fs.mkdirSync(dir, { recursive: true });
    const payload = JSON.stringify(
      {
        version: 1,
        storage: process.platform === "win32" ? "dpapi" : "os-keychain",
        entries,
      },
      null,
      2
    );
    const tmp = `${this.filePath}.tmp`;
    fs.writeFileSync(tmp, payload, { encoding: "utf8", mode: 0o600 });
    fs.renameSync(tmp, this.filePath);
  }

  listSecretNames() {
    return Object.keys(this._readRaw()).sort();
  }

  setSecret(name, value) {
    const key = String(name || "").trim();
    if (!ENV_KEY_RE.test(key)) {
      return { ok: false, error: `Invalid secret key name: "${key}"` };
    }
    if (!this.isAvailable()) {
      return { ok: false, error: "OS credential encryption (DPAPI) is unavailable." };
    }
    const secret = String(value ?? "");
    if (!secret) {
      return this.deleteSecret(key);
    }
    const enc = safeStorage.encryptString(secret);
    const entries = this._readRaw();
    entries[key] = enc.toString("base64");
    this._writeRaw(entries);
    return {
      ok: true,
      storage: process.platform === "win32" ? "dpapi" : "os-keychain",
    };
  }

  deleteSecret(name) {
    const key = String(name || "").trim();
    if (!key) return { ok: true };
    const entries = this._readRaw();
    if (Object.prototype.hasOwnProperty.call(entries, key)) {
      delete entries[key];
      this._writeRaw(entries);
    }
    return { ok: true };
  }

  /**
   * Main-process only: decrypt all stored secrets into a plain env map
   * for spawning the local FastAPI backend subprocess.
   */
  getDecryptedEnv() {
    if (!this.isAvailable()) return {};
    const entries = this._readRaw();
    const out = {};
    for (const [k, b64] of Object.entries(entries)) {
      if (!ENV_KEY_RE.test(k) || typeof b64 !== "string" || !b64) continue;
      try {
        const plain = safeStorage.decryptString(Buffer.from(b64, "base64"));
        if (plain) out[k] = plain;
      } catch (_) {
        // Skip corrupt or undecryptable entries cleanly.
      }
    }
    return out;
  }
}

module.exports = { SecretVault };
