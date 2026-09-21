// A place in the sidebar that a page can fill. The Files page uses it to put
// its folders, places and storage meter in the sidebar instead of a second
// column beside it - so the screen has one navigation column, not two.
import { useSyncExternalStore } from 'react';

let el = null;
const subs = new Set();

export function setSidebarSlot(node) {
  if (el === node) return;
  el = node;
  subs.forEach((fn) => fn());
}

export function useSidebarSlot() {
  return useSyncExternalStore(
    (fn) => { subs.add(fn); return () => subs.delete(fn); },
    () => el,
    () => null,
  );
}
