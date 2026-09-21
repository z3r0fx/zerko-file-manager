import { useEffect, useSyncExternalStore } from 'react';
import { apiCall } from './api';

/**
 * Where each photo sits inside its 16:9 window (0 = top / left, 1 = bottom /
 * right, missing = centred). One shared copy: the grid tiles, the viewer and
 * the export dialog all read the same numbers, so what you frame is what you
 * see everywhere.
 */
let map = {};
let loadedOnce = false;
let inflight = null;
const subs = new Set();

const emit = () => subs.forEach((f) => f());
const subscribe = (f) => { subs.add(f); return () => subs.delete(f); };

export function ensureCrops(force = false) {
  if (loadedOnce && !force) return Promise.resolve(map);
  if (inflight) return inflight;
  inflight = apiCall('/api/photos/crops')
    .then((data) => { map = data || {}; loadedOnce = true; emit(); return map; })
    .catch(() => map)          // older backend / signed out: everything stays centred
    .finally(() => { inflight = null; });
  return inflight;
}

export const getCrop = (id) => (map[id] === undefined ? 0.5 : map[id]);

/** Remember framing on the server. `changes` is { id: 0..1 | null }. */
export async function saveCrops(changes) {
  const body = {};
  Object.entries(changes).forEach(([id, v]) => { body[id] = v == null || Math.abs(v - 0.5) < 0.0005 ? null : v; });
  if (!Object.keys(body).length) return;
  await apiCall('/api/photos/crops', { method: 'PUT', body: JSON.stringify({ crops: body }) });
  const next = { ...map };
  Object.entries(body).forEach(([id, v]) => { if (v === null) delete next[id]; else next[id] = v; });
  map = next;
  emit();
}

/** Framing for one photo. Re-renders only that photo's tile when it changes. */
export function useCrop(id) {
  useEffect(() => { ensureCrops(); }, []);
  const v = useSyncExternalStore(subscribe, () => map[id]);
  return v === undefined ? 0.5 : v;
}
