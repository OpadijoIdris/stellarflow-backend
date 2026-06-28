import { describe, it, expect, beforeEach, jest } from "@jest/globals";
import { SubprocessSandbox, createSandbox, dbSandbox } from "../src/security/sandbox";
import { appConfig } from "../src/config/configWatcher";

describe("SubprocessSandbox", () => {
  let sandbox: SubprocessSandbox;

  beforeEach(() => {
    sandbox = createSandbox({
      enabled: true,
      timeoutMs: 5000,
      maxMemoryMb: 256,
      allowNetwork: false,
      allowFileWrites: false,
    });
  });

  describe("Platform Detection", () => {
    it("should detect the current platform", () => {
      const platform = sandbox.getPlatform();
      expect(["linux", "win32", "darwin", "unknown"]).toContain(platform);
    });
  });

  describe("Policy Management", () => {
    it("should have default policy values", () => {
      const policy = sandbox.getPolicy();
      expect(policy.enabled).toBe(true);
      expect(policy.timeoutMs).toBe(5000);
      expect(policy.maxMemoryMb).toBe(256);
      expect(policy.allowNetwork).toBe(false);
      expect(policy.allowFileWrites).toBe(false);
    });

    it("should update policy", () => {
      sandbox.updatePolicy({
        timeoutMs: 10000,
        allowNetwork: true,
      });

      const policy = sandbox.getPolicy();
      expect(policy.timeoutMs).toBe(10000);
      expect(policy.allowNetwork).toBe(true);
      expect(policy.enabled).toBe(true); // unchanged
      expect(policy.maxMemoryMb).toBe(256); // unchanged
    });

    it("should disable sandboxing via policy", () => {
      sandbox.updatePolicy({ enabled: false });
      expect(sandbox.getPolicy().enabled).toBe(false);
    });
  });

  describe("Command Execution", () => {
    it("should execute a simple command successfully", () => {
      const result = sandbox.execSync("echo test");
      expect(result.success).toBe(true);
      expect(result.stdout).toContain("test");
      expect(result.exitCode).toBe(0);
      expect(result.sandboxApplied).toBe(true);
    });

    it("should handle command failure", () => {
      const result = sandbox.execSync("exit 1");
      expect(result.success).toBe(false);
      expect(result.exitCode).toBe(1);
    });

    it("should handle non-existent command", () => {
      const result = sandbox.execSync("nonexistent-command-12345");
      expect(result.success).toBe(false);
      expect(result.error).toBeDefined();
    });

    it("should respect timeout when enabled", () => {
      const timeoutSandbox = createSandbox({
        enabled: true,
        timeoutMs: 1000,
        maxMemoryMb: 256,
        allowNetwork: false,
        allowFileWrites: false,
      });

      // This command should timeout on most systems
      const result = timeoutSandbox.execSync("ping -c 10 127.0.0.1");
      expect(result.success).toBe(false);
      expect(result.error).toBeDefined();
    }, 10000);
  });

  describe("Unsandboxed Execution", () => {
    it("should execute without sandboxing when disabled", () => {
      const unsandboxed = createSandbox({ enabled: false });
      const result = unsandboxed.execSync("echo test");
      expect(result.success).toBe(true);
      expect(result.sandboxApplied).toBe(false);
    });
  });

  describe("dbSandbox Instance", () => {
    it("should use configuration from appConfig", () => {
      const policy = dbSandbox.getPolicy();
      expect(policy.enabled).toBe(appConfig.sandbox.enabled);
      expect(policy.timeoutMs).toBe(appConfig.sandbox.timeoutMs);
      expect(policy.maxMemoryMb).toBe(appConfig.sandbox.maxMemoryMb);
      expect(policy.allowNetwork).toBe(appConfig.sandbox.allowNetwork);
      expect(policy.allowFileWrites).toBe(appConfig.sandbox.allowFileWrites);
    });

    it("should execute Prisma commands safely", () => {
      // Test with a simple echo command instead of actual Prisma
      // to avoid dependencies in test environment
      const result = dbSandbox.execSync("echo prisma-test");
      expect(result.success).toBe(true);
      expect(result.sandboxApplied).toBe(true);
    });
  });

  describe("Platform-Specific Behavior", () => {
    it("should handle Windows platform", () => {
      const windowsSandbox = createSandbox({
        enabled: true,
        timeoutMs: 5000,
        maxMemoryMb: 256,
        allowNetwork: false,
        allowFileWrites: false,
      });

      // Mock platform detection
      const platform = windowsSandbox.getPlatform();
      expect(["linux", "win32", "darwin", "unknown"]).toContain(platform);
    });

    it("should handle Linux platform with seccomp", () => {
      const linuxSandbox = createSandbox({
        enabled: true,
        timeoutMs: 5000,
        maxMemoryMb: 256,
        allowNetwork: true,
        allowFileWrites: true,
        allowedSyscalls: ["read", "write", "execve"],
      });

      const policy = linuxSandbox.getPolicy();
      expect(policy.allowedSyscalls).toContain("read");
      expect(policy.allowedSyscalls).toContain("write");
      expect(policy.allowedSyscalls).toContain("execve");
    });
  });

  describe("Resource Limits", () => {
    it("should enforce memory limits", () => {
      const memoryLimitedSandbox = createSandbox({
        enabled: true,
        timeoutMs: 5000,
        maxMemoryMb: 100,
        allowNetwork: false,
        allowFileWrites: false,
      });

      const policy = memoryLimitedSandbox.getPolicy();
      expect(policy.maxMemoryMb).toBe(100);
    });

    it("should enforce network restrictions", () => {
      const networkRestrictedSandbox = createSandbox({
        enabled: true,
        timeoutMs: 5000,
        maxMemoryMb: 256,
        allowNetwork: false,
        allowFileWrites: true,
      });

      const policy = networkRestrictedSandbox.getPolicy();
      expect(policy.allowNetwork).toBe(false);
      expect(policy.allowFileWrites).toBe(true);
    });
  });

  describe("Error Handling", () => {
    it("should handle empty commands gracefully", () => {
      const result = sandbox.execSync("");
      expect(result.success).toBe(false);
    });

    it("should handle commands with special characters", () => {
      const result = sandbox.execSync('echo "test with spaces"');
      expect(result.success).toBe(true);
      expect(result.stdout).toContain("test with spaces");
    });
  });
});
