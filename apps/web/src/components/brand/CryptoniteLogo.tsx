// Generated from docs/brand/svg (cryptonite-symbol-small-reversed + cryptonite-wordmark-reversed).
// The geometry lives in docs/brand/build_kit.py; regenerate there instead of editing these paths.
// Colours come from the --brand-* tokens in globals.css.

import { cn } from "@/lib/utils";

interface CryptoniteLogoProps {
  className?: string;
  /** Classes for the symbol (sets its height; width follows). */
  symbolClassName?: string;
  /** Classes for the wordmark, e.g. "hidden sm:block" to show the symbol alone on small screens. */
  wordmarkClassName?: string;
}

/** Cryptonite logo for dark surfaces: the Crystal Candle symbol (small-size cut) plus the custom wordmark. */
export function CryptoniteLogo({ className, symbolClassName, wordmarkClassName }: CryptoniteLogoProps) {
  return (
    <span role="img" aria-label="Cryptonite" className={cn("inline-flex items-center gap-2", className)}>
      <svg
        viewBox="53 20 150 216"
        className={cn("h-7 w-auto flex-none", symbolClassName)}
        aria-hidden="true"
        focusable="false"
      >
        <path d="M117 20 L139 20 L139 56 L117 56 Z" fill="var(--brand-quartz)" />
        <path d="M116.5 56 L52.5 120 L52.5 136 L116.5 200 Z" fill="var(--brand-kryptonite)" />
        <path d="M139.5 56 L203.5 120 L203.5 136 L139.5 200 Z" fill="var(--brand-facet)" />
        <path d="M117 200 L139 200 L139 236 L117 236 Z" fill="var(--brand-quartz)" />
      </svg>
      <svg
        viewBox="12 12 853 100"
        className={cn("h-3 w-auto flex-none", wordmarkClassName)}
        aria-hidden="true"
        focusable="false"
      >
        <path d="M96 42 L96 38 L70 12 L38 12 L12 38 L12 86 L38 112 L70 112 L96 86 L96 82 L75.54 82 L62.54 95 L45.46 95 L30 79.54 L30 44.46 L45.46 29 L62.54 29 L75.54 42 Z" fill="var(--brand-quartz)" />
        <path d="M114 12 L170.06 12 L192 33.94 L192 52.06 L170.06 74 L192 112 L171.22 112 L149.28 74 L132 74 L132 112 L114 112 Z M132 29 L162.6 29 L174 40.4 L174 45.6 L162.6 57 L132 57 Z" fill="var(--brand-quartz)" fillRule="evenodd" />
        <path d="M204 12 L224.78 12 L246 48.75 L267.22 12 L288 12 L255 69.16 L255 112 L237 112 L237 69.16 Z" fill="var(--brand-quartz)" />
        <path d="M299 12 L351.06 12 L373 33.94 L373 52.06 L351.06 74 L317 74 L317 112 L299 112 Z M317 29 L343.6 29 L355 40.4 L355 45.6 L343.6 57 L317 57 Z" fill="var(--brand-quartz)" fillRule="evenodd" />
        <path d="M380 12 L456 12 L456 29 L427 29 L427 112 L409 112 L409 29 L380 29 Z" fill="var(--brand-quartz)" />
        <path d="M491 12 L527 12 L553 38 L553 86 L527 112 L491 112 L465 86 L465 38 Z M498.46 29 L519.54 29 L535 44.46 L535 79.54 L519.54 95 L498.46 95 L483 79.54 L483 44.46 Z" fill="var(--brand-quartz)" fillRule="evenodd" />
        <path d="M571 12 L593.86 12 L633.6 80.82 L633.6 12 L651.6 12 L651.6 112 L628.74 112 L589 43.18 L589 112 L571 112 Z" fill="var(--brand-quartz)" />
        <path d="M672.6 12 L690.6 12 L690.6 112 L672.6 112 Z" fill="var(--brand-quartz)" />
        <path d="M705.6 12 L781.6 12 L781.6 29 L752.6 29 L752.6 112 L734.6 112 L734.6 29 L705.6 29 Z" fill="var(--brand-quartz)" />
        <path d="M796.6 12 L862.6 12 L862.6 29 L814.6 29 L814.6 51.5 L856.6 51.5 L856.6 68.5 L814.6 68.5 L814.6 95 L862.6 95 L862.6 112 L796.6 112 Z" fill="var(--brand-quartz)" />
      </svg>
    </span>
  );
}
