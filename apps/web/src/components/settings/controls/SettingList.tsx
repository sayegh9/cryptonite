"use client";

import React, { useState } from "react";
import { cn } from "@/lib/utils";
import { SettingCard, type RegistryEntry } from "./SettingCard";

export interface SettingListProps {
  entries: RegistryEntry[];
  /** The full registry, for the cross-field exit rules */
  allEntries?: RegistryEntry[];
  onSaved: () => void;
  /** The row to ring, scroll to and open (a jump from global search or the exit gauge) */
  highlightedKey?: string | null;
  /** Ring every row (search results); no row opens */
  highlightAll?: boolean;
  className?: string;
}

/**
 * A stack of setting rows. Local accordion: one editor open at a time. Nothing here persists.
 */
export const SettingList: React.FC<SettingListProps> = ({
  entries,
  allEntries,
  onSaved,
  highlightedKey,
  highlightAll = false,
  className,
}) => {
  const hasKey = (key?: string | null) => Boolean(key) && entries.some((e) => e.key === key);
  const [openKey, setOpenKey] = useState<string | null>(hasKey(highlightedKey) ? (highlightedKey ?? null) : null);
  const [seenHighlight, setSeenHighlight] = useState<string | null | undefined>(highlightedKey);

  // A new jump target opens its row (adjusting state while rendering, not in an effect)
  if (seenHighlight !== highlightedKey) {
    setSeenHighlight(highlightedKey);
    if (hasKey(highlightedKey)) setOpenKey(highlightedKey ?? null);
  }

  return (
    <div className={cn("grid gap-2", className)}>
      {entries.map((entry) => (
        <SettingCard
          key={entry.key}
          entry={entry}
          allEntries={allEntries}
          onSaved={onSaved}
          highlight={highlightAll || highlightedKey === entry.key}
          open={openKey === entry.key}
          onOpenChange={(next) => setOpenKey((k) => (next ? entry.key : k === entry.key ? null : k))}
        />
      ))}
    </div>
  );
};

export default SettingList;
