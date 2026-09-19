import { useState, useEffect, useCallback } from 'react';
import { apiCall } from '../../lib/api';
import { HardDrive, AlertTriangle, Copy, Film, Image, Music, FileText, Layers } from 'lucide-react';

/*
 * Where the storage is going.
 *
 * Three questions in the order they matter: how full is the drive, what kind
 * of thing is filling it, and which sources are the heavy ones.
 *
 * Deliberately NOT a pie chart. Comparing angles is harder than comparing bar
 * lengths, and with eight sources of wildly different size a pie turns into
 * slivers. Horizontal bars sorted by size answer "what is biggest" instantly.
 *
 * One hue throughout: this is ONE measure (bytes) across categories, so length
 * carries the magnitude and colour would only add noise. The capacity bar is
 * the exception - there, two segments genuinely mean different things, and
 * those two colours are validated against this surface for colour-blind
 * separation.
 */

// Validated against the app's dark surface (#18181b): lightness band, chroma
// floor, CVD separation and contrast all pass.
const C = {
  library: '#d95926',   // this app's files
  other:   '#3987e5',   // everything else on the drive
  bar:     '#d95926',   // single-hue magnitude
  track:   'rgba(255,255,255,0.07)',
};

const TYPE_ICON = { video: Film, photo: Image, audio: Music, document: FileText, project: Layers };

const fmtPct = (n) => (n < 1 ? '<1' : Math.round(n));

export default function StoragePanel() {
  const [d, setD] = useState(null);
  const [tab, setTab] = useState('source');

  const load = useCallback(async () => {
    try { setD(await apiCall('/api/storage/breakdown')); } catch { /* keep last */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  if (!d) return null;

  const disk = d.disk || {};
  const hasDisk = !disk.error && disk.total;
  const pctUsed = disk.percent_used ?? 0;
  const nearlyFull = pctUsed >= 85;

  const rows = tab === 'source' ? (d.by_source || []) : (d.by_folder || []);
  const max = Math.max(1, ...rows.map((r) => r.bytes));

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <h2 className="mb-4 flex items-center gap-2 font-semibold text-zinc-200">
        <HardDrive className="h-5 w-5 text-zinc-400" /> Storage
      </h2>

      {/* ---- the headline: how full is the drive ---- */}
      {hasDisk && (
        <div className="mb-5">
          <div className="mb-2 flex flex-wrap items-baseline gap-x-2">
            <span className="text-3xl font-semibold tracking-tight text-zinc-100">
              {fmtPct(pctUsed)}%
            </span>
            <span className="text-sm text-zinc-400">
              of {disk.total_formatted} used
            </span>
            <span className="ml-auto text-sm text-zinc-400">
              <strong className="text-zinc-100">{disk.free_formatted}</strong> free
            </span>
          </div>

          {/* capacity bar: two real segments on an empty track */}
          <div className="flex h-3 w-full gap-[2px] overflow-hidden rounded-full"
               style={{ background: C.track }}
               role="img"
               aria-label={`${disk.library_formatted} this library, ${disk.other_formatted} other files, ${disk.free_formatted} free of ${disk.total_formatted}`}>
            <div style={{ width: `${(disk.library_bytes / disk.total) * 100}%`, background: C.library }}
                 title={`This library — ${disk.library_formatted}`} />
            <div style={{ width: `${(disk.other_bytes / disk.total) * 100}%`, background: C.other }}
                 title={`Other files on the drive — ${disk.other_formatted}`} />
          </div>

          {/* identity never by colour alone */}
          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs">
            <span className="flex items-center gap-1.5 text-zinc-300">
              <span className="h-2 w-2 rounded-sm" style={{ background: C.library }} />
              This library <span className="font-mono text-zinc-500">{disk.library_formatted}</span>
            </span>
            <span className="flex items-center gap-1.5 text-zinc-300">
              <span className="h-2 w-2 rounded-sm" style={{ background: C.other }} />
              Other files <span className="font-mono text-zinc-500">{disk.other_formatted}</span>
            </span>
            <span className="flex items-center gap-1.5 text-zinc-500">
              <span className="h-2 w-2 rounded-sm" style={{ background: C.track }} />
              Free <span className="font-mono">{disk.free_formatted}</span>
            </span>
          </div>

          {nearlyFull && (
            <p className="mt-2 flex items-center gap-1.5 text-xs text-amber-400">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              Under {disk.free_formatted} left — worth clearing space before the next shoot.
            </p>
          )}
        </div>
      )}

      {/* ---- what kind of thing is it ---- */}
      <div className="mb-5 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {(d.by_type || []).map((t) => {
          const Icon = TYPE_ICON[t.name] || Layers;
          return (
            <div key={t.name} className="rounded border border-zinc-800 bg-zinc-950/50 p-2.5">
              <p className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-zinc-500">
                <Icon className="h-3 w-3" /> {t.name}
              </p>
              <p className="mt-1 text-sm font-medium text-zinc-100">{t.formatted}</p>
              <p className="font-mono text-[10px] text-zinc-600">
                {t.files.toLocaleString()} files · avg {t.avg_formatted}
              </p>
            </div>
          );
        })}
      </div>

      {/* ---- where it is going ---- */}
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-sm text-zinc-300">Where the space is going</h3>
        <div className="ml-auto flex gap-1">
          {[['source', 'By camera'], ['folder', 'By folder']].map(([k, label]) => (
            <button key={k} onClick={() => setTab(k)}
                    className={'rounded px-2.5 py-1 text-xs transition '
                      + (tab === k ? 'bg-zinc-100 text-zinc-900' : 'text-zinc-500 hover:text-zinc-200')}>
              {label}
            </button>
          ))}
        </div>
      </div>

      <ul className="space-y-1.5">
        {rows.slice(0, 8).map((r) => (
          <li key={r.name} title={`${r.files} files · average ${r.avg_formatted}`}>
            <div className="mb-0.5 flex items-baseline justify-between gap-2 text-xs">
              <span className="truncate text-zinc-300">{r.name}</span>
              <span className="shrink-0 font-mono text-zinc-400">{r.formatted}</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="h-1.5 flex-1 overflow-hidden rounded-full" style={{ background: C.track }}>
                <div className="h-full rounded-full"
                     style={{ width: `${(r.bytes / max) * 100}%`, background: C.bar }} />
              </div>
              <span className="w-28 shrink-0 text-right font-mono text-[10px] text-zinc-600">
                {r.files} × {r.avg_formatted}
              </span>
            </div>
          </li>
        ))}
      </ul>

      {/* ---- the actionable bit ---- */}
      {(d.reclaimable > 0 || d.proxy_bytes > 0) && (
        <div className="mt-4 flex flex-wrap gap-x-6 gap-y-1 border-t border-zinc-800 pt-3 text-xs">
          {d.reclaimable > 0 && (
            <span className="flex items-center gap-1.5 text-zinc-400">
              <Copy className="h-3.5 w-3.5 text-[#d95926]" />
              <strong className="font-mono text-zinc-200">{d.reclaimable_formatted}</strong>
              in duplicates you could reclaim
            </span>
          )}
          {d.proxy_bytes > 0 && (
            <span className="text-zinc-500">
              proxies take <span className="font-mono">{d.proxy_formatted}</span>
            </span>
          )}
        </div>
      )}
    </div>
  );
}
