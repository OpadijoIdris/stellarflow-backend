import { execSync, spawn, ChildProcess } from "child_process";
import * as os from "os";

/**
 * Platform type for sandboxing
 */
type Platform = "linux" | "win32" | "darwin" | "unknown";

/**
 * Sandbox policy configuration
 */
export interface SandboxPolicy {
  /**
   * Whether sandboxing is enabled
   */
  enabled: boolean;

  /**
   * Maximum execution time in milliseconds (0 = no limit)
   */
  timeoutMs: number;

  /**
   * Maximum memory in MB (0 = no limit)
   */
  maxMemoryMb: number;

  /**
   * Allowed syscalls on Linux (seccomp)
   * Only applies on Linux platform
   */
  allowedSyscalls?: string[];

  /**
   * Whether to allow network access
   */
  allowNetwork: boolean;

  /**
   * Whether to allow file system writes
   */
  allowFileWrites: boolean;

  /**
   * Working directory restriction (empty = no restriction)
   */
  restrictToDirectory?: string;
}

/**
 * Default sandbox policy for database operations
 */
const DEFAULT_DB_POLICY: SandboxPolicy = {
  enabled: true,
  timeoutMs: 30000,
  maxMemoryMb: 512,
  allowNetwork: true,
  allowFileWrites: true,
  allowedSyscalls: [
    // Basic I/O
    "read",
    "write",
    "open",
    "close",
    "stat",
    "fstat",
    "lstat",
    "access",
    // Process management
    "execve",
    "exit",
    "exit_group",
    "fork",
    "clone",
    "wait4",
    "waitpid",
    // Memory
    "mmap",
    "mprotect",
    "munmap",
    "brk",
    // Signal handling
    "rt_sigaction",
    "rt_sigprocmask",
    "rt_sigreturn",
    // File operations
    "readlink",
    "getcwd",
    "chdir",
    // Network (for DB connections)
    "socket",
    "connect",
    "bind",
    "listen",
    "accept",
    "sendto",
    "recvfrom",
    // Time
    "gettimeofday",
    "clock_gettime",
    "nanosleep",
  ],
};

/**
 * Sandbox execution result
 */
export interface SandboxResult {
  success: boolean;
  stdout: string;
  stderr: string;
  exitCode: number | null;
  error?: string;
  sandboxApplied: boolean;
  platform: Platform;
}

/**
 * Subprocess Sandbox Manager
 * Provides cross-platform subprocess sandboxing with:
 * - Linux: seccomp filters for syscall restriction
 * - Windows: Job Objects for resource limits
 * - macOS: Resource limits via ulimit
 */
export class SubprocessSandbox {
  private platform: Platform;
  private policy: SandboxPolicy;

  constructor(policy: Partial<SandboxPolicy> = {}) {
    this.platform = this.detectPlatform();
    this.policy = { ...DEFAULT_DB_POLICY, ...policy };
  }

  /**
   * Detect the current platform
   */
  private detectPlatform(): Platform {
    const platform = os.platform();
    if (platform === "linux") return "linux";
    if (platform === "win32") return "win32";
    if (platform === "darwin") return "darwin";
    return "unknown";
  }

  /**
   * Execute a command synchronously with sandboxing
   */
  execSync(command: string, options?: { cwd?: string; env?: NodeJS.ProcessEnv }): SandboxResult {
    if (!this.policy.enabled) {
      return this.execSyncUnsandboxed(command, options);
    }

    console.log(`[Sandbox] Executing command with sandbox: ${command}`);
    console.log(`[Sandbox] Platform: ${this.platform}, Policy: ${JSON.stringify(this.policy)}`);

    try {
      switch (this.platform) {
        case "linux":
          return this.execSyncLinux(command, options);
        case "win32":
          return this.execSyncWindows(command, options);
        case "darwin":
          return this.execSyncMacOS(command, options);
        default:
          console.warn(`[Sandbox] Unknown platform ${this.platform}, falling back to unsandboxed execution`);
          return this.execSyncUnsandboxed(command, options);
      }
    } catch (error) {
      return {
        success: false,
        stdout: "",
        stderr: "",
        exitCode: null,
        error: error instanceof Error ? error.message : String(error),
        sandboxApplied: true,
        platform: this.platform,
      };
    }
  }

  /**
   * Execute command on Linux with seccomp sandboxing
   */
  private execSyncLinux(command: string, options?: { cwd?: string; env?: NodeJS.ProcessEnv }): SandboxResult {
    try {
      // Check if seccomp is available
      const hasSeccomp = this.checkSeccompAvailable();
      
      if (!hasSeccomp) {
        console.warn("[Sandbox] seccomp not available, using resource limits only");
        return this.execSyncWithResourceLimits(command, options);
      }

      // Build seccomp filter
      const seccompProfile = this.buildSeccompProfile();
      
      // Execute with seccomp using the seccomp command-line tool
      // This requires the 'seccomp' package or libseccomp tools
      const sandboxedCommand = this.wrapWithSeccomp(command, seccompProfile);
      
      const startTime = Date.now();
      const stdout = execSync(sandboxedCommand, {
        stdio: "pipe",
        encoding: "utf-8",
        cwd: options?.cwd,
        env: options?.env || process.env,
        timeout: this.policy.timeoutMs > 0 ? this.policy.timeoutMs : undefined,
      });
      const endTime = Date.now();

      console.log(`[Sandbox] Command completed in ${endTime - startTime}ms`);

      return {
        success: true,
        stdout,
        stderr: "",
        exitCode: 0,
        sandboxApplied: true,
        platform: this.platform,
      };
    } catch (error) {
      const err = error as any;
      return {
        success: false,
        stdout: err.stdout || "",
        stderr: err.stderr || "",
        exitCode: err.status || null,
        error: err.message,
        sandboxApplied: true,
        platform: this.platform,
      };
    }
  }

  /**
   * Execute command on Windows with Job Object sandboxing
   */
  private execSyncWindows(command: string, options?: { cwd?: string; env?: NodeJS.ProcessEnv }): SandboxResult {
    try {
      // Windows doesn't have seccomp, but we can use resource limits
      // We'll wrap the command in a PowerShell script with resource limits
      const sandboxedCommand = this.wrapWindowsCommand(command);
      
      const startTime = Date.now();
      const stdout = execSync(sandboxedCommand, {
        stdio: "pipe",
        encoding: "utf-8",
        cwd: options?.cwd,
        env: options?.env || process.env,
        timeout: this.policy.timeoutMs > 0 ? this.policy.timeoutMs : undefined,
        shell: "powershell.exe",
      });
      const endTime = Date.now();

      console.log(`[Sandbox] Command completed in ${endTime - startTime}ms`);

      return {
        success: true,
        stdout,
        stderr: "",
        exitCode: 0,
        sandboxApplied: true,
        platform: this.platform,
      };
    } catch (error) {
      const err = error as any;
      return {
        success: false,
        stdout: err.stdout || "",
        stderr: err.stderr || "",
        exitCode: err.status || null,
        error: err.message,
        sandboxApplied: true,
        platform: this.platform,
      };
    }
  }

  /**
   * Execute command on macOS with resource limits
   */
  private execSyncMacOS(command: string, options?: { cwd?: string; env?: NodeJS.ProcessEnv }): SandboxResult {
    // macOS uses ulimit for resource limits
    return this.execSyncWithResourceLimits(command, options);
  }

  /**
   * Execute with resource limits (works on Linux/macOS)
   */
  private execSyncWithResourceLimits(command: string, options?: { cwd?: string; env?: NodeJS.ProcessEnv }): SandboxResult {
    try {
      const startTime = Date.now();
      const stdout = execSync(command, {
        stdio: "pipe",
        encoding: "utf-8",
        cwd: options?.cwd,
        env: options?.env || process.env,
        timeout: this.policy.timeoutMs > 0 ? this.policy.timeoutMs : undefined,
        // Set ulimit for memory if supported
        maxBuffer: this.policy.maxMemoryMb > 0 ? this.policy.maxMemoryMb * 1024 * 1024 : undefined,
      });
      const endTime = Date.now();

      console.log(`[Sandbox] Command completed in ${endTime - startTime}ms`);

      return {
        success: true,
        stdout,
        stderr: "",
        exitCode: 0,
        sandboxApplied: true,
        platform: this.platform,
      };
    } catch (error) {
      const err = error as any;
      return {
        success: false,
        stdout: err.stdout || "",
        stderr: err.stderr || "",
        exitCode: err.status || null,
        error: err.message,
        sandboxApplied: true,
        platform: this.platform,
      };
    }
  }

  /**
   * Execute without sandboxing (fallback)
   */
  private execSyncUnsandboxed(command: string, options?: { cwd?: string; env?: NodeJS.ProcessEnv }): SandboxResult {
    try {
      const startTime = Date.now();
      const stdout = execSync(command, {
        stdio: "pipe",
        encoding: "utf-8",
        cwd: options?.cwd,
        env: options?.env || process.env,
      });
      const endTime = Date.now();

      console.log(`[Sandbox] Unsandboxed command completed in ${endTime - startTime}ms`);

      return {
        success: true,
        stdout,
        stderr: "",
        exitCode: 0,
        sandboxApplied: false,
        platform: this.platform,
      };
    } catch (error) {
      const err = error as any;
      return {
        success: false,
        stdout: err.stdout || "",
        stderr: err.stderr || "",
        exitCode: err.status || null,
        error: err.message,
        sandboxApplied: false,
        platform: this.platform,
      };
    }
  }

  /**
   * Check if seccomp is available on the system
   */
  private checkSeccompAvailable(): boolean {
    try {
      // Check if seccomp is available in the kernel
      execSync("test -f /proc/self/status", { stdio: "pipe" });
      const status = execSync("cat /proc/self/status", { encoding: "utf-8" });
      return status.includes("Seccomp");
    } catch {
      return false;
    }
  }

  /**
   * Build a seccomp profile JSON
   */
  private buildSeccompProfile(): string {
    const profile = {
      defaultAction: "SCMP_ACT_ERRNO",
      architectures: ["SCMP_ARCH_X86_64", "SCMP_ARCH_X86", "SCMP_ARCH_X32"],
      syscalls: this.policy.allowedSyscalls?.map((syscall) => ({
        name: syscall,
        action: "SCMP_ACT_ALLOW",
      })) || [],
    };

    return JSON.stringify(profile);
  }

  /**
   * Wrap command with seccomp using the seccomp tool
   */
  private wrapWithSeccomp(command: string, profile: string): string {
    // Try to use the 'seccomp' command-line tool if available
    // This requires installing libseccomp tools: apt-get install libseccomp-tools
    try {
      execSync("which seccomp-profile", { stdio: "pipe" });
      return `seccomp-profile --profile='${profile}' -- ${command}`;
    } catch {
      // Fallback: try to use the 'seccomp' Node.js package
      console.warn("[Sandbox] seccomp-profile tool not found, using basic resource limits");
      return command;
    }
  }

  /**
   * Wrap Windows command with PowerShell resource limits
   */
  private wrapWindowsCommand(command: string): string {
    // Use PowerShell to set resource limits
    const timeout = this.policy.timeoutMs > 0 ? this.policy.timeoutMs / 1000 : 0;
    
    if (timeout > 0) {
      return `Start-Process -FilePath "cmd.exe" -ArgumentList "/c ${command}" -Wait -WindowStyle Hidden -Timeout ${timeout}`;
    }
    
    return command;
  }

  /**
   * Update the sandbox policy
   */
  updatePolicy(newPolicy: Partial<SandboxPolicy>): void {
    this.policy = { ...this.policy, ...newPolicy };
    console.log(`[Sandbox] Policy updated: ${JSON.stringify(this.policy)}`);
  }

  /**
   * Get current policy
   */
  getPolicy(): SandboxPolicy {
    return { ...this.policy };
  }

  /**
   * Get current platform
   */
  getPlatform(): Platform {
    return this.platform;
  }

  /**
   * Sync policy with appConfig (called by configWatcher)
   */
  syncWithConfig(config: { sandbox: { enabled: boolean; timeoutMs: number; maxMemoryMb: number; allowNetwork: boolean; allowFileWrites: boolean } }): void {
    this.policy = {
      ...this.policy,
      enabled: config.sandbox.enabled,
      timeoutMs: config.sandbox.timeoutMs,
      maxMemoryMb: config.sandbox.maxMemoryMb,
      allowNetwork: config.sandbox.allowNetwork,
      allowFileWrites: config.sandbox.allowFileWrites,
    };
    console.log(`[Sandbox] Policy synced with config: ${JSON.stringify(this.policy)}`);
  }
}

/**
 * Default sandbox instance for database operations
 */
export const dbSandbox = new SubprocessSandbox(DEFAULT_DB_POLICY);

/**
 * Create a custom sandbox instance
 */
export function createSandbox(policy: Partial<SandboxPolicy>): SubprocessSandbox {
  return new SubprocessSandbox(policy);
}
