import { toast } from 'sonner';
import { fetchApi } from '@/lib/api';
import { refreshPortfolio, type PaperPosition } from '@/lib/stores/usePortfolioStore';
import { isPerp } from './metrics';

const csrf = () => ({ 'X-CSRF-Token': localStorage.getItem('csrf_token') || '' });

/** POST a paper command; surfaces failures as a toast and always resyncs. Returns success. */
export async function postCommand(path: string, body?: unknown, failureTitle = 'Command rejected'): Promise<boolean> {
  try {
    await fetchApi(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body), headers: csrf() });
    return true;
  } catch (e) {
    toast.error(failureTitle, { description: e instanceof Error ? e.message : String(e) });
    return false;
  } finally {
    void refreshPortfolio();
  }
}

export const closePosition = (instanceId: string, instrumentId: string) =>
  postCommand(`/paper/strategies/${instanceId}/close-position`, { instrument_id: instrumentId }, `Close ${instrumentId} failed`);

const exitSide = (p: PaperPosition) => (p.side === 'SHORT' ? 'BUY' : 'SELL');
const floorQty = (q: number) => Math.floor(q * 1e6) / 1e6;

/** Cut the position by `fraction` with an opposing market order (reduce-only on perps). */
export function reducePosition(p: PaperPosition, fraction = 0.5) {
  const quantity = floorQty(p.quantity * fraction);
  if (quantity <= 0) {
    toast.error(`Reduce ${p.instrument_id}: position too small to split`);
    return Promise.resolve(false);
  }
  const perp = isPerp(p.instrument_id);
  return postCommand('/paper/orders', {
    instrument_id: p.instrument_id,
    side: exitSide(p),
    order_type: 'MARKET',
    quantity,
    ...(perp ? { reduce_only: true, leverage: p.leverage ?? undefined, margin_mode: p.margin_mode ?? undefined } : {}),
  }, `Reduce ${p.instrument_id} failed`);
}

/** Perps only: flatten, then open the same size on the other side at the same leverage. */
export async function reversePosition(p: PaperPosition) {
  if (!isPerp(p.instrument_id)) return false;
  // Sequential on purpose: the orchestrator drains commands in creation order
  if (!(await closePosition(p.instance_id, p.instrument_id))) return false;
  return postCommand('/paper/orders', {
    instrument_id: p.instrument_id,
    side: exitSide(p),
    order_type: 'MARKET',
    quantity: floorQty(p.quantity),
    leverage: p.leverage ?? undefined,
    margin_mode: p.margin_mode ?? undefined,
  }, `Reverse ${p.instrument_id}: re-entry failed after close`);
}
