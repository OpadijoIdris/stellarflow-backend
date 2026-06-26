import promClient from "prom-client";
import { register } from "./middleware/metrics.js";

export const successfulSubmissions = new promClient.Counter({
  name: "multisig_successful_submissions_total",
  help: "Total number of successful multi-sig submissions to Stellar",
  labelNames: ["asset"],
});
register.registerMetric(successfulSubmissions);

export const failedSubmissions = new promClient.Counter({
  name: "multisig_failed_submissions_total",
  help: "Total number of failed multi-sig submissions to Stellar",
  labelNames: ["asset", "reason"],
});
register.registerMetric(failedSubmissions);

export const gasUsagePerAsset = new promClient.Histogram({
  name: "multisig_gas_usage_stroops",
  help: "Gas usage in stroops per submitted multi-sig transaction",
  labelNames: ["asset"],
  buckets: [100, 500, 1000, 5000, 10000, 50000, 100000],
});
register.registerMetric(gasUsagePerAsset);

export const submissionDuration = new promClient.Histogram({
  name: "multisig_submission_duration_seconds",
  help: "Duration of multi-sig submission operations in seconds",
  labelNames: ["asset"],
  buckets: [0.1, 0.5, 1, 2, 5, 10, 30],
});
register.registerMetric(submissionDuration);
