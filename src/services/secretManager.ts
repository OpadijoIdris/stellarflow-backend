import { Keypair } from "@stellar/stellar-sdk";
import { logger } from "../utils/logger";
import { vault } from "../crypto/vault";
import { decrypt } from "../crypto/encryption";

export type ReloadTrigger = "admin-endpoint" | "file-watcher" | "startup";

// Module-level private state
let reloadCount: number = 0;
const KEY_SLOT = "stellar-secret";

/**
 * Validates a candidate Stellar secret key.
 * Throws with a safe message — never includes the candidate value.
 */
function validateKey(candidate: string): void {
  if (!candidate || candidate.trim().length === 0) {
    throw new Error("Secret key must not be empty");
  }

  try {
    Keypair.fromSecret(candidate);
  } catch {
    throw new Error("Invalid Stellar secret key format");
  }
}

/**
 * Initialization function — strict validation on startup.
 * Called at the end of this module.
 */
function init(): void {
  const isKms = process.env.SIGNER_BACKEND === "kms";
  if (isKms) {
    logger.info("[SecretManager] Running in KMS mode. Local keys bypassed.");
    return;
  }

  const plaintextKey =
    process.env.STELLAR_SECRET ||
    process.env.ORACLE_SECRET_KEY ||
    process.env.SOROBAN_ADMIN_SECRET;
  const encryptedKey = process.env.ENCRYPTED_STELLAR_SECRET;
  const masterKey = process.env.VAULT_MASTER_KEY;

  let finalKey: string | undefined;

  try {
    if (encryptedKey) {
      if (!masterKey) {
        throw new Error(
          "[SecretManager] ENCRYPTED_STELLAR_SECRET is set but VAULT_MASTER_KEY is missing.",
        );
      }
      logger.info("[SecretManager] Attempting to decrypt STELLAR_SECRET...");
      finalKey = decrypt(encryptedKey, masterKey);
    } else if (plaintextKey) {
      logger.warn(
        "[SecretManager] Using plaintext secret key from .env. (Production Violation)",
      );
      finalKey = plaintextKey;
    }

    if (!finalKey) {
      if (process.env.NODE_ENV === "test" || process.env.CI === "true") {
        logger.warn("[SecretManager] No signing key found — skipping in test/CI environment.");
        return;
      }
      console.error("❌ [SecretManager] CRITICAL: No signing key found in environment variables.");
      console.error("Please set STELLAR_SECRET or ENCRYPTED_STELLAR_SECRET.");
      process.exit(1);
    }

    validateKey(finalKey);
    vault.register(KEY_SLOT, finalKey);
    logger.info(
      "[SecretManager] Signing key successfully loaded into secure vault.",
    );
  } catch (err: any) {
    if (process.env.NODE_ENV === "test" || process.env.CI === "true") {
      logger.warn(`[SecretManager] Key load failed in test/CI — skipping: ${err.message}`);
      return;
    }
    console.error(`❌ [SecretManager] CRITICAL: Failed to load signing key: ${err.message}`);
    process.exit(1);
  }
}

// Run initialization
init();

/**
 * Returns the currently active Stellar secret key from the vault.
 */
export function getSecretKey(): string {
  if (process.env.SIGNER_BACKEND === "kms") {
    throw new Error("Secret key is not available in KMS mode");
  }

  const context = vault.openContext("secret-retrieval");
  try {
    return vault.retrieve(KEY_SLOT, context);
  } finally {
    vault.closeContext(context);
  }
}

/**
 * Returns the public key derived from the currently active signer.
 */
export function getPublicKey(): string {
  if (process.env.SIGNER_BACKEND === "kms") {
    return process.env.STELLAR_PUBLIC_KEY || "KMS_MANAGED_KEY";
  }

  const secret = getSecretKey();
  return Keypair.fromSecret(secret).publicKey();
}

/**
 * Returns the number of successful key updates since module load.
 */
export function getReloadCount(): number {
  return reloadCount;
}

/**
 * Validates and atomically replaces the in-vault secret key.
 */
export function updateSecretKey(
  newKey: string,
  trigger: ReloadTrigger = "admin-endpoint",
): void {
  if (process.env.SIGNER_BACKEND === "kms") {
    throw new Error("Secret key updates are disabled in KMS mode");
  }

  try {
    validateKey(newKey);
    const newPublicKey = Keypair.fromSecret(newKey).publicKey();

    vault.revoke(KEY_SLOT);
    vault.register(KEY_SLOT, newKey);
    reloadCount += 1;

    logger.info("[SecretManager] Key reloaded successfully.", "SecretManager", {
      trigger,
      publicKey: newPublicKey,
      reloadCount,
      timestamp: new Date().toISOString(),
    });
  } catch (err: any) {
    logger.warn("[SecretManager] Key reload rejected.", "SecretManager", {
      trigger,
      reason: err.message,
      timestamp: new Date().toISOString(),
    });
    throw err;
  }
}
