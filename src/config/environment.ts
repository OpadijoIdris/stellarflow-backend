/**
 * Environment sanitization helpers for startup.
 *
 * Startup environment values often come from external sources such as .env
 * files or deployment pipelines. We normalize token-like values so a mixed-case
 * setting like "ngN" or "Kes" is stored in a predictable lowercase form.
 * This helps avoid runtime mismatches for asset codes and other case-insensitive
 * environment tokens while preserving sensitive values and URLs.
 */

const SENSITIVE_ENV_KEY_PATTERNS = [
  /SECRET/i,
  /TOKEN/i,
  /PASSWORD/i,
  /_URL$/i,
  /_KEY$/i,
  /^DATABASE_/i,
  /^JWT_/i,
  /^SESSION_/i,
];

const CASE_PRESERVE_ENV_KEYS = new Set<string>([
  "STELLAR_NETWORK",
]);

const SAFE_TOKEN_VALUE = /^[A-Za-z0-9,_-]+$/;

function isSensitiveEnvKey(key: string): boolean {
  return SENSITIVE_ENV_KEY_PATTERNS.some((pattern) => pattern.test(key));
}

function isSafeTokenValue(value: string): boolean {
  return SAFE_TOKEN_VALUE.test(value);
}

export function normalizeEnvValue(value: string | undefined): string | undefined {
  if (value === undefined) {
    return undefined;
  }

  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}

export function sanitizeEnvironmentVariables(): void {
  for (const [key, rawValue] of Object.entries(process.env)) {
    if (typeof rawValue !== "string") {
      continue;
    }

    const normalizedValue = normalizeEnvValue(rawValue);
    if (normalizedValue === undefined) {
      process.env[key] = undefined;
      continue;
    }

    if (
      !CASE_PRESERVE_ENV_KEYS.has(key) &&
      isSafeTokenValue(normalizedValue) &&
      !isSensitiveEnvKey(key)
    ) {
      process.env[key] = normalizedValue.toLowerCase();
      continue;
    }

    process.env[key] = normalizedValue;
  }
}
