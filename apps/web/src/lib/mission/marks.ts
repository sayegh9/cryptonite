/** B3 (F-H3): copy for a position or mandate valued at a stored mark because no live quote is fresh. */
export function staleMarkText(ageS: number | null | undefined): string {
  if (ageS === null || ageS === undefined) return "Marks stale: no live quote, valued at the last stored price";
  return `Marks stale: no live quote, last marked ${ageS < 120 ? `${Math.round(ageS)} s` : `${Math.round(ageS / 60)} min`} ago`;
}
