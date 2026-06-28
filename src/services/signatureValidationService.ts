import prisma from "../lib/prisma";
import { Keypair } from "@stellar/stellar-sdk";
import { Request } from "express";
import { logger } from "../utils/logger";
import { generateKsuid } from "../utils/ksuid.js";

export interface AdminSignature {
  adminPublicKey: string;
  adminName: string;
  adminRole: string;
  signature: string; // Hex encoded
  ipAddress: string;
  userAgent?: string | undefined;
}

export interface ConsensusRequest {
  actionType: string;
  actionData?: string;
  requestedBy: string;
  requiredSignatures?: number;
  expiresAt?: Date;
}

export interface ValidationResult {
  valid: boolean;
  canExecute: boolean;
  message: string;
  pendingSignatures?: AdminSignature[];
  missingSignatures?: number;
}

export class SignatureValidationService {
  // eslint-disable-next-line @typescript-eslint/naming-convention
  private readonly CONSENSUS_EXPIRY_HOURS = 24;
  // eslint-disable-next-line @typescript-eslint/naming-convention
  private readonly MIN_REQUIRED_SIGNATURES = 2;
  // eslint-disable-next-line @typescript-eslint/naming-convention
  private readonly MAX_REQUIRED_SIGNATURES = 5;

  /**
   * Create a new pending consensus request
   */
  async createConsensusRequest(
    request: ConsensusRequest,
    req: Request,
  ): Promise<{ id: number; status: string }> {
    const requiredSignatures = Math.min(
      Math.max(
        request.requiredSignatures || this.MIN_REQUIRED_SIGNATURES,
        this.MIN_REQUIRED_SIGNATURES,
      ),
      this.MAX_REQUIRED_SIGNATURES,
    );

    const expiresAt =
      request.expiresAt ||
      new Date(Date.now() + this.CONSENSUS_EXPIRY_HOURS * 60 * 60 * 1000);

    const pendingConsensus = await prisma.pendingConsensus.create({
      data: {
        actionType: request.actionType,
        actionData: request.actionData ?? null,
        status: "PENDING",
        requiredSignatures,
        collectedSignatures: 0,
        requestedBy: request.requestedBy,
        requestedAt: new Date(),
        expiresAt,
      },
    });

    await this.logAuditEvent({
      eventType: "CONSENSUS_INITIATED",
      actionType: request.actionType,
      relatedId: pendingConsensus.id,
      actorPublicKey: request.requestedBy,
      actorName: this.extractAdminName(req),
      actorRole: this.extractAdminRole(req),
      eventDetails: JSON.stringify({
        actionData: request.actionData,
        requiredSignatures,
        expiresAt,
      }),
      ipAddress: this.extractIpAddress(req),
      userAgent: req.get("User-Agent"),
    });

    return { id: pendingConsensus.id, status: pendingConsensus.status };
  }

  /**
   * Add an admin signature to a pending consensus request
   */
  async addSignature(
    consensusId: number,
    adminSignature: AdminSignature,
    req: Request,
  ): Promise<ValidationResult> {
    const consensus = await prisma.pendingConsensus.findUnique({
      where: { id: consensusId },
      include: { pendingSignatures: true },
    });

    if (!consensus)
      return {
        valid: false,
        canExecute: false,
        message: "Consensus request not found",
      };
    if (consensus.status !== "PENDING")
      return {
        valid: false,
        canExecute: false,
        message: `Consensus is ${consensus.status}`,
      };
    if (new Date() > consensus.expiresAt) {
      await this.updateConsensusStatus(consensusId, "EXPIRED");
      return {
        valid: false,
        canExecute: false,
        message: "Consensus request has expired",
      };
    }

    if (
      consensus.pendingSignatures.some(
        (sig) => sig.adminPublicKey === adminSignature.adminPublicKey,
      )
    ) {
      return {
        valid: false,
        canExecute: false,
        message: "Admin has already signed",
      };
    }

    const isSignatureValid = await this.validateSignature(
      consensusId,
      consensus.actionType,
      adminSignature.signature,
      adminSignature.adminPublicKey,
      consensus.expiresAt,
    );

    if (!isSignatureValid) {
      await this.logAuditEvent({
        eventType: "SIGNATURE_INVALID",
        actionType: consensus.actionType,
        relatedId: consensusId,
        actorPublicKey: adminSignature.adminPublicKey,
        actorName: adminSignature.adminName,
        actorRole: adminSignature.adminRole,
        ipAddress: adminSignature.ipAddress,
        userAgent: adminSignature.userAgent,
      });
      return { valid: false, canExecute: false, message: "Invalid signature" };
    }

    const updatedConsensus = await prisma.$transaction(async (tx) => {
      await tx.pendingSignature.create({
        data: {
          pendingConsensusId: consensusId,
          adminPublicKey: adminSignature.adminPublicKey,
          adminName: adminSignature.adminName,
          adminRole: adminSignature.adminRole,
          signature: adminSignature.signature,
          ipAddress: adminSignature.ipAddress,
          userAgent: adminSignature.userAgent || null,
          signedAt: new Date(),
        },
      });

      return tx.pendingConsensus.update({
        where: { id: consensusId },
        data: { collectedSignatures: { increment: 1 } },
        include: { pendingSignatures: true },
      });
    });

    const canExecute =
      updatedConsensus.collectedSignatures >=
      updatedConsensus.requiredSignatures;
    if (canExecute) await this.updateConsensusStatus(consensusId, "APPROVED");

    return {
      valid: true,
      canExecute,
      message: canExecute
        ? "Consensus reached - action can be executed"
        : `Signature added. Need ${updatedConsensus.requiredSignatures - updatedConsensus.collectedSignatures} more signatures`,
      pendingSignatures: updatedConsensus.pendingSignatures.map((sig: any) => ({
        adminPublicKey: sig.adminPublicKey,
        adminName: sig.adminName,
        adminRole: sig.adminRole,
        signature: sig.signature,
        ipAddress: sig.ipAddress,
        userAgent: sig.userAgent || undefined,
      })),
      missingSignatures: Math.max(
        0,
        updatedConsensus.requiredSignatures -
          updatedConsensus.collectedSignatures,
      ),
    };
  }

  /**
   * Refactored for Issue #370: Optimized Signature Validation
   * Reduces heap allocations by using deterministic message construction
   */
  private async validateSignature(
    id: number,
    type: string,
    sigHex: string,
    pubKey: string,
    expiresAt: Date,
  ): Promise<boolean> {
    try {
      // Deterministic message string - minimize string concatenations
      const msg = `SF-CONSENSUS-${id}-${type}-${expiresAt.getTime()}`;

      const keypair = Keypair.fromPublicKey(pubKey);
      const sigBuffer = Buffer.from(sigHex, "hex");
      const msgBuffer = Buffer.from(msg, "utf-8");

      return keypair.verify(msgBuffer, sigBuffer);
    } catch (err) {
      logger.error(`[SignatureValidation] Failed for ${pubKey}:`, err);
      return false;
    }
  }

  async validateConsensus(consensusId: number): Promise<ValidationResult> {
    const consensus = await prisma.pendingConsensus.findUnique({
      where: { id: consensusId },
      include: { pendingSignatures: true },
    });

    if (!consensus || consensus.status !== "APPROVED") {
      return {
        valid: false,
        canExecute: false,
        message: "Invalid consensus state",
      };
    }

    // Secondary verification check
    for (const sig of consensus.pendingSignatures) {
      const isValid = await this.validateSignature(
        consensus.id,
        consensus.actionType,
        sig.signature,
        sig.adminPublicKey,
        consensus.expiresAt,
      );
      if (!isValid)
        return {
          valid: false,
          canExecute: false,
          message: "Integrity check failed",
        };
    }

    return { valid: true, canExecute: true, message: "Consensus validated" };
  }

  async markAsExecuted(
    consensusId: number,
    result: string,
    req: Request,
  ): Promise<void> {
    await prisma.pendingConsensus.update({
      where: { id: consensusId },
      data: {
        status: "EXECUTED",
        executedAt: new Date(),
        executionResult: result,
      },
    });
  }

  private async updateConsensusStatus(
    id: number,
    status: string,
  ): Promise<void> {
    await prisma.pendingConsensus.update({ where: { id }, data: { status } });
  }

  private async logAuditEvent(event: any): Promise<void> {
    await prisma.auditLog.create({
      data: { ...event, id: generateKsuid(), occurredAt: new Date() },
    });
  }

  async getConsensusRequest(consensusId: number) {
    return prisma.pendingConsensus.findUnique({
      where: { id: consensusId },
      include: { pendingSignatures: true },
    });
  }

  async getPendingRequests() {
    return prisma.pendingConsensus.findMany({
      where: { status: "PENDING" },
      include: { pendingSignatures: true },
    });
  }

  private extractAdminName(req: Request): string {
    return (req as any).admin?.name || "unknown";
  }
  private extractAdminRole(req: Request): string {
    return (req as any).admin?.role || "unknown";
  }
  private extractIpAddress(req: Request): string {
    return req.ip || "0.0.0.0";
  }
}

export const signatureValidationService = new SignatureValidationService();
