import Transport from 'winston-transport';
import axios from 'axios';

interface HttpTransportOptions {
  level?: string;
}

export class HttpLogTransport extends Transport {
  private queue: any[] = [];

  private flushing = false;

  constructor(
    opts: HttpTransportOptions,
  ) {
    super(opts);

    setInterval(
      () => this.flush(),
      3000,
    );
  }

  log(info: any, callback: () => void) {
    setImmediate(() => {
      this.emit('logged', info);
    });

    this.queue.push({
      timestamp:
        new Date().toISOString(),
      level: info.level,
      message: info.message,
      meta: info.meta ?? {},
    });

    callback();
  }

  private async flush() {
    if (
      this.flushing ||
      this.queue.length === 0
    ) {
      return;
    }

    this.flushing = true;

    const batch =
      this.queue.splice(0, 20);

    try {
      await axios.post(
        process.env.LOG_STREAM_URL!,
        batch,
        {
          timeout: Number(
            process.env
              .LOG_STREAM_TIMEOUT_MS ??
              2000,
          ),

          headers: {
            Authorization:
              `Bearer ${process.env.LOG_STREAM_TOKEN}`,
            'Content-Type':
              'application/json',
          },
        },
      );
    } catch (error) {
      console.error(
        '[log-stream] failed to stream logs',
      );

      // Requeue logs to avoid loss
      this.queue.unshift(...batch);
    } finally {
      this.flushing = false;
    }
  }
}