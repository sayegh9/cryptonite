/**
 * WebSocket Stream Client for Cryptonite Live Market Feed.
 * Connects exclusively to the API Gateway at /ws/stream.
 */

type MessageHandler = (data: any) => void;

export class CryptoniteStreamClient {
  private ws: WebSocket | null = null;
  private url: string;
  private listeners: Set<MessageHandler> = new Set();
  private reconnectTimer: NodeJS.Timeout | null = null;
  private isExplicitClose: boolean = false;
  private retryCount: number = 0;
  private activeSubscriptions: { channels: string[]; instruments?: string[] } | null = null;

  constructor(url?: string) {
    if (url) {
      this.url = url;
    } else if (typeof window !== "undefined") {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const host = window.location.hostname || "127.0.0.1";
      this.url = `${protocol}//${host}:8000/ws/stream`;
    } else {
      this.url = "ws://127.0.0.1:8000/ws/stream";
    }
  }

  public connect(subscriptions?: { channels: string[]; instruments?: string[] }): void {
    if (subscriptions) {
      this.activeSubscriptions = subscriptions;
    }
    if (typeof window === "undefined") return;

    this.isExplicitClose = false;
    try {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => {
        this.retryCount = 0;
        if (this.activeSubscriptions) {
          this.send({
            action: "subscribe",
            channels: this.activeSubscriptions.channels,
            instruments: this.activeSubscriptions.instruments,
          });
        }
      };

      this.ws.onmessage = (event) => {
        try {
          const parsed = JSON.parse(event.data);
          this.listeners.forEach((listener) => {
            try {
              listener(parsed);
            } catch (err) {
              console.error("[StreamClient] Listener error:", err);
            }
          });
        } catch (err) {
          console.error("[StreamClient] Failed to parse message:", err);
        }
      };

      this.ws.onerror = (err) => {
        console.warn("[StreamClient] WebSocket encountered error:", err);
      };

      this.ws.onclose = (event) => {
        if (!this.isExplicitClose) {
          const delay = Math.min(1000 * Math.pow(2, this.retryCount), 10000);
          this.retryCount++;
          this.reconnectTimer = setTimeout(() => {
            this.connect();
          }, delay);
        }
      };
    } catch (err) {
      console.error("[StreamClient] Failed to create WebSocket:", err);
    }
  }

  public subscribe(handler: MessageHandler): () => void {
    this.listeners.add(handler);
    return () => {
      this.listeners.delete(handler);
    };
  }

  public send(payload: any): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
    }
  }

  public updateSubscriptions(channels: string[], instruments?: string[]): void {
    this.activeSubscriptions = { channels, instruments };
    this.send({
      action: "subscribe",
      channels,
      instruments,
    });
  }

  public disconnect(): void {
    this.isExplicitClose = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}

// Singleton stream instance for the client
let globalClient: CryptoniteStreamClient | null = null;

export function getStreamClient(): CryptoniteStreamClient {
  if (!globalClient) {
    globalClient = new CryptoniteStreamClient();
  }
  return globalClient;
}
