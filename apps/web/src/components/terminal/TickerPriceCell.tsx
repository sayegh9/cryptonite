"use client";

import React, { useRef, useEffect } from "react";

interface TickerPriceCellProps {
  price: number | null;
  previousPrice?: number | null;
  className?: string;
  currencyPrefix?: string;
  decimals?: number;
}

export function TickerPriceCell({
  price,
  previousPrice,
  className = "",
  currencyPrefix = "$",
  decimals = 2,
}: TickerPriceCellProps) {
  const cellRef = useRef<HTMLSpanElement>(null);
  const prevRef = useRef<number | null>(previousPrice ?? null);

  useEffect(() => {
    if (price === null || prevRef.current === null || !cellRef.current) {
      prevRef.current = price;
      return;
    }

    const prev = prevRef.current;
    prevRef.current = price;

    if (price === prev) return;

    const el = cellRef.current;
    el.classList.remove("flash-tick-up", "flash-tick-down");

    // Force layout reflow so the animation restarts smoothly
    void el.offsetWidth;

    if (price > prev) {
      el.classList.add("flash-tick-up");
    } else {
      el.classList.add("flash-tick-down");
    }
  }, [price]);

  if (price === null) {
    return <span className={`font-mono text-xs tabular-nums text-zinc-500 ${className}`}>--</span>;
  }

  return (
    <span
      ref={cellRef}
      className={`inline-block px-1.5 py-0.5 rounded font-mono text-xs font-semibold tabular-nums text-zinc-100 transition-colors ${className}`}
    >
      {currencyPrefix}
      {price.toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })}
    </span>
  );
}
