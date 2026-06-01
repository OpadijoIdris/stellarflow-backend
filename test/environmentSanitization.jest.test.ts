import {
  normalizeEnvValue,
  sanitizeEnvironmentVariables,
} from "../src/config/environment";

describe("Environment sanitization", () => {
  const originalEnv = { ...process.env };

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it("trims whitespace and normalizes token-like env values to lowercase", () => {
    process.env.SIGNER_BACKEND = "  KMS  ";
    process.env.NOTIFICATION_PLATFORMS = "DISCORD,SLACK";
    process.env.MULTI_SIG_ENABLED = " TRUE ";

    sanitizeEnvironmentVariables();

    expect(process.env.SIGNER_BACKEND).toBe("kms");
    expect(process.env.NOTIFICATION_PLATFORMS).toBe("discord,slack");
    expect(process.env.MULTI_SIG_ENABLED).toBe("true");
  });

  it("preserves sensitive env values and URLs while trimming whitespace", () => {
    process.env.JWT_SECRET = "  Secret123  ";
    process.env.REDIS_URL = "  HTTPS://localhost:6379  ";

    sanitizeEnvironmentVariables();

    expect(process.env.JWT_SECRET).toBe("Secret123");
    expect(process.env.REDIS_URL).toBe("HTTPS://localhost:6379");
  });

  it("does not modify STELLAR_NETWORK casing", () => {
    process.env.STELLAR_NETWORK = "public";

    sanitizeEnvironmentVariables();

    expect(process.env.STELLAR_NETWORK).toBe("public");
  });

  it("returns undefined for empty values and lowercases safe token values", () => {
    expect(normalizeEnvValue("  NGN ")).toBe("NGN");
    expect(normalizeEnvValue("   ")).toBeUndefined();
  });
});
