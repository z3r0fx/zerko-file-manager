import { useSyncExternalStore } from 'react';

/**
 * Which tab (All / Videos / Photos / Audio) the library is showing. The tab
 * lives inside the library page; the sidebar needs to know it too, so it can
 * list only the folders that hold that kind of media.
 */
let current = 'all';
const subs = new Set();
export function setBrowsingType(t) {
  const next = t || 'all';
  if (next === current) return;
  current = next;
  subs.forEach((f) => f());
}
export function useBrowsingType() {
  return useSyncExternalStore((f) => { subs.add(f); return () => subs.delete(f); }, () => current);
}
