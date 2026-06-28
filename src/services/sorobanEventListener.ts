import { BackpressureManager, PacketPriority } from "../queue/backpressure";
import { Horizon } from "@stellar/stellar-sdk";
import type { ServerApi } from "@stellar/stellar-sdk/lib/horizon";
import prisma from "../lib/prisma";
import { broadcastToSessions } from "../lib/socket";
import stellarProvider from "../lib/stellarProvider";
import dotenv from "dotenv";
import { logger } from "../utils/logger";
import { parseBase64ToPositiveNumber } from "../serialization/helpers.js";

dotenv.config();

export interface ConfirmedPrice {
  currency: string;
  rate: number;
  txHash: string;
  memoId: string | null;
  ledgerSeq: number;
  confirmedAt: Date;
}

export class SorobanEventListener {
  private bpManager = new BackpressureManager();
  private server: Horizon.Server;
  private oraclePublicKey: string;
  private isRunning: boolean = false;
  private pollIntervalMs: number;
  private lastProcessedLedger: number = 0;
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  constructor(pollIntervalMs: number = 15000) {
    const secret =
      process.env.ORACLE_SECRET_KEY ??
      process.env.SOROBAN_ADMIN_SECRET ??
      process.env.STELLAR_SECRET;
    if (!secret) {
      throw new Error(
        "Stellar secret key not found in environment variables. Please set STELLAR_SECRET or SOROBAN_ADMIN_SECRET.",
      );
    }
    this.oraclePublicKey = "";
    this.pollIntervalMs = pollIntervalMs;
    this.server = stellarProvider.getServer();
  }

  getOraclePublicKey(): string {
    return this.oraclePublicKey;
  }

  async start(): Promise<void> {
    if (this.isRunning) {
      logger.warn("[EventListener] SorobanEventListener is already running");
      return;
    }

    this.isRunning = true;
    const { signer } = await import("../signer/index.js");
    this.oraclePublicKey = await signer.getPublicKey();

    logger.info(
      `[EventListener] Starting listener for account ${this.oraclePublicKey}`,
    );

    const lastRecord = await prisma.onChainPrice.findFirst({
      orderBy: { ledgerSeq: "desc" },
    });

    if (lastRecord) {
      this.lastProcessedLedger = lastRecord.ledgerSeq;
      logger.info(
        `[EventListener] Resuming from ledger ${this.lastProcessedLedger}`,
      );
    }

    // Start the background worker to process the backpressure queue
    this.startWorker();

    // Initial poll
    await this.pollTransactions();

    // Start periodic polling
    this.pollTimer = setInterval(() => {
      this.pollTransactions().catch((err) => {
        logger.networkError("[EventListener] Poll error:", { err });
      });
    }, this.pollIntervalMs);
  }

  /**
   * Worker loop that processes packets from the queue at a controlled pace.
   */
  private async startWorker(): Promise<void> {
    logger.info("[Worker] Backpressure consumer loop started.");
    while (this.isRunning) {
      const packet = await this.bpManager.dequeue();

      if (packet) {
        try {
          const price = packet.data as ConfirmedPrice;

          if (packet.priority === PacketPriority.STANDARD) {
            // Essential data: Save to DB
            await prisma.onChainPrice.create({
              data: {
                currency: price.currency,
                rate: price.rate,
                txHash: price.txHash,
                memoId: price.memoId,
                ledgerSeq: price.ledgerSeq,
                confirmedAt: price.confirmedAt,
              },
            });
          }

          // Broadcast all successful updates (Essential or Metric) to UI
          broadcastToSessions("price_update", price);
        } catch (err) {
          logger.error("[Worker] Failed to process queued price:", err);
        }
      } else {
        // Wait 100ms if queue is empty to prevent CPU spinning
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }
  }

  private async pollTransactions(): Promise<void> {
    try {
      this.server = stellarProvider.getServer();

      const transactions = await this.server
        .transactions()
        .forAccount(this.oraclePublicKey)
        .order("desc")
        .limit(50)
        .call();

      for (const tx of transactions.records) {
        if (tx.ledger_attr <= this.lastProcessedLedger) continue;

        const memoId = this.extractMemoId(tx);
        if (!memoId || !memoId.startsWith("SF-")) continue;

        const prices = await this.parseOperations(tx, memoId);

        for (const price of prices) {
          // Wrap price in a packet and send to queue
          const packet = {
            priority: PacketPriority.STANDARD, // Using Standard for financial data
            data: price,
            timestamp: Date.now(),
          };

          const accepted = await this.bpManager.enqueue(packet);
          if (accepted) {
            // Update tracking only if it was accepted by queue
            if (price.ledgerSeq > this.lastProcessedLedger) {
              this.lastProcessedLedger = price.ledgerSeq;
            }
          }
        }
      }
    } catch (error) {
      stellarProvider.reportFailure(error);
      if (error instanceof Error && error.message.includes("status code 404"))
        return;
      throw error;
    }
  }

  // ... (Keep extractMemoId and parseOperations methods as they were) ...

  private extractMemoId(tx: ServerApi.TransactionRecord): string | null {
    if (tx.memo_type === "text" && tx.memo) return tx.memo;
    return null;
  }

  private async parseOperations(
    tx: ServerApi.TransactionRecord,
    memoId: string,
  ): Promise<ConfirmedPrice[]> {
    const confirmedPrices: ConfirmedPrice[] = [];
    try {
      const operations = await tx.operations();
      for (const op of operations.records) {
        if (op.type !== "manage_data") continue;
        const manageDataOp = op as ServerApi.ManageDataOperationRecord;
        if (!manageDataOp.name.endsWith("_PRICE")) continue;

        const currency = manageDataOp.name.replace("_PRICE", "");
        const valueBase64 = manageDataOp.value;
        if (!valueBase64) continue;

        const rate = parseFloat(atob(String(valueBase64)));
        if (isNaN(rate)) continue;

        confirmedPrices.push({
          currency,
          rate,
          txHash: tx.hash,
          memoId,
          ledgerSeq: tx.ledger_attr,
          confirmedAt: new Date(tx.created_at),
        });
      }
    } catch (error) {
      logger.networkError(`[EventListener] Error parsing tx ${tx.hash}:`, {
        error,
      });
    }
    return confirmedPrices;
  }

  stop(): void {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.isRunning = false;
    logger.info("[EventListener] Stopped");
  }

  restart(pollIntervalMs?: number): void {
    this.stop();
    if (pollIntervalMs !== undefined) this.pollIntervalMs = pollIntervalMs;
    this.start().catch((err) =>
      logger.error("[EventListener] Restart failed:", err),
    );
  }

  isActive(): boolean {
    return this.isRunning;
  }
}
