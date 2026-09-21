export type CrosshairData = { sourceId: string; time: number | null; price: number | null };
type Listener = (data: CrosshairData) => void;

class CrosshairEmitter {
  private listeners: Set<Listener> = new Set();
  
  emit(data: CrosshairData) {
    this.listeners.forEach(l => l(data));
  }
  
  subscribe(listener: Listener) {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }
}

export const crosshairEmitter = new CrosshairEmitter();
