import { createLogger, format, transports } from "winston";
import * as path from "path";
import DailyRotateFile from "winston-daily-rotate-file";

const logDir = path.resolve(process.cwd(), "logs");

// Custom filters to separate network and system logs
const networkFilter = format((info) => {
  return info.isNetwork ? info : false;
});

const systemFilter = format((info) => {
  return info.isNetwork ? false : info;
});

const structuredJsonFormat = format.combine(
  format.timestamp({ format: "YYYY-MM-DDTHH:mm:ss.SSSZ" }),
  format.errors({ stack: true }),
  format.splat(),
  format((info) => {
    info.module_name = info.module_name ?? info.label ?? "app";

    if (info.error instanceof Error) {
      const err = info.error as Error & { code?: string | number };
      info.error = {
        name: err.name,
        message: err.message,
        stack: err.stack,
        code: err.code,
      };
      info.error_code = info.error_code ?? err.code;
    }

    if (!info.error && info.stack) {
      info.error = {
        message: info.message,
        stack: info.stack,
      };
    }

    if (info.error_code === undefined && typeof info.code !== "undefined") {
      info.error_code = info.code;
    }

    return info;
  })(),
  format.json(),
);

const logger = createLogger({
  level: "info",
  transports: [
    new DailyRotateFile({
      filename: path.join(logDir, "system-%DATE%.log"),
      datePattern: "YYYY-MM-DD",
      maxSize: "100m",
      maxFiles: "10",
      zippedArchive: true,
      handleExceptions: true,
      handleRejections: true,
      format: format.combine(systemFilter(), structuredJsonFormat),
    }),
    new DailyRotateFile({
      filename: path.join(logDir, "stellar-network-%DATE%.log"),
      datePattern: "YYYY-MM-DD",
      maxSize: "100m",
      maxFiles: "10",
      zippedArchive: true,
      format: format.combine(networkFilter(), structuredJsonFormat),
    }),
    new transports.Console({
      format: structuredJsonFormat,
      handleExceptions: true,
      handleRejections: true,
    }),
  ],
  exitOnError: false,
});

// Add custom methods for fetcher-specific logging
(logger as any).fetcherError = (message: string, meta?: any) => {
  logger.error(`[FETCHER_ERROR] ${message}`, meta);
};

// Add custom methods for network boundary logging
(logger as any).networkInfo = (message: string, meta?: any) => {
  logger.info(`[NETWORK] ${message}`, { ...meta, isNetwork: true });
};

(logger as any).networkError = (message: string, meta?: any) => {
  logger.error(`[NETWORK_ERROR] ${message}`, { ...meta, isNetwork: true });
};

export default logger;
