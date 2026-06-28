import fs from "fs";
import path from "path";
import { dbSandbox } from "../security/sandbox";

export interface RateLimitConfig {
  /** Rolling window duration in milliseconds (default: 900_000 = 15 min) */
  windowMs: number;
  /** Maximum requests per IP per window (default: 100) */
  maxRequests: number;
  /** Whether global rate limiting is active (default: true) */
  enabled: boolean;
}

export interface SandboxConfig {
  /** Whether subprocess sandboxing is enabled (default: true) */
  enabled: boolean;
  /** Maximum execution time in milliseconds (default: 30000 = 30 sec) */
  timeoutMs: number;
  /** Maximum memory in MB (default: 512) */
  maxMemoryMb: number;
  /** Whether to allow network access (default: true) */
  allowNetwork: boolean;
  /** Whether to allow file system writes (default: true) */
  allowFileWrites: boolean;
}

export interface AppConfig {
  fetchIntervalMs: number;
  sorobanPollIntervalMs: number;
  multiSigPollIntervalMs: number;
  hourlyAverageCheckIntervalMs: number;
  cacheDurationMs: number;
  batchWindowMs: number;
  rateLimit: RateLimitConfig;
  sandbox: SandboxConfig;
}

export const CONFIG_PATH = path.resolve(process.cwd(), "config.json");

const DEFAULTS: AppConfig = {
  fetchIntervalMs: 10000,
  sorobanPollIntervalMs: 15000,
  multiSigPollIntervalMs: 30000,
  hourlyAverageCheckIntervalMs: 900000,
  cacheDurationMs: 30000,
  batchWindowMs: 5000,
  rateLimit: {
    windowMs: 900000,
    maxRequests: 100,
    enabled: true,
  },
  sandbox: {
    enabled: true,
    timeoutMs: 30000,
    maxMemoryMb: 512,
    allowNetwork: true,
    allowFileWrites: true,
  },
};

function loadConfig(): AppConfig {
  try {
    const raw = fs.readFileSync(CONFIG_PATH, "utf-8");
    const parsed: Partial<AppConfig> = JSON.parse(raw);
    return {
      ...DEFAULTS,
      ...parsed,
      rateLimit: { ...DEFAULTS.rateLimit, ...(parsed.rateLimit ?? {}) },
      sandbox: { ...DEFAULTS.sandbox, ...(parsed.sandbox ?? {}) },
    };
  } catch {
    return { ...DEFAULTS, rateLimit: { ...DEFAULTS.rateLimit }, sandbox: { ...DEFAULTS.sandbox } };
  }
}

// Singleton in-memory config — mutated on reload
export const appConfig: AppConfig = loadConfig();

/**
 * Starts a fs.watch watcher on config.json.
 * On change, merges the new values into the shared `appConfig` object so all
 * consumers that hold a reference to it see the update immediately.
 * Calls the optional `onChange` callback with the updated config after each reload.
 * Returns a cleanup function that stops the watcher.
 */
export function watchConfig(
  onChange?: (config: AppConfig) => void,
): () => void {
  if (!fs.existsSync(CONFIG_PATH)) {
    console.warn(
      `[ConfigWatcher] config.json not found at ${CONFIG_PATH}. Hot-reload disabled.`,
    );
    return () => {};
  }

  const watcher = fs.watch(CONFIG_PATH, (event: string) => {
    if (event !== "change") return;
    try {
      const raw = fs.readFileSync(CONFIG_PATH, "utf-8");
      const updated: Partial<AppConfig> = JSON.parse(raw);
      Object.assign(appConfig, DEFAULTS, updated);
      if (updated.rateLimit) {
        Object.assign(
          appConfig.rateLimit,
          DEFAULTS.rateLimit,
          updated.rateLimit,
        );
      }
      if (updated.sandbox) {
        Object.assign(
          appConfig.sandbox,
          DEFAULTS.sandbox,
          updated.sandbox,
        );
        // Sync sandbox policy with new config
        try {
          dbSandbox.syncWithConfig(appConfig);
        } catch (err) {
          console.error("[ConfigWatcher] Failed to sync sandbox policy:", err);
        }
      }
      console.info("[ConfigWatcher] config.json reloaded:", appConfig);
      onChange?.(appConfig);
    } catch (err) {
      console.error("[ConfigWatcher] Failed to reload config.json:", err);
    }
  });

  console.info(`[ConfigWatcher] Watching ${CONFIG_PATH} for changes`);
  return () => watcher.close();
}
