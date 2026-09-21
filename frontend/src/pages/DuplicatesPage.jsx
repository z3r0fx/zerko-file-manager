import { useState, useEffect, useCallback } from 'react';
import { Copy, HardDrive, RefreshCw, Trash2, ShieldCheck, AlertTriangle, Zap, ArrowRight, ChevronDown, ChevronRight as ChevRight } from 'lucide-react';
import { apiCall } from '../lib/api';

/**
 * Duplicate review.
 *
 * Nothing is deleted without an explicit choice per group. The default keep is
 * the FIRST file (lowest id = the one indexed earliest), but every option is a
 * radio button, because only you know which copy is the one your projects
 * actually point at.
 */
export default function DuplicatesPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [keep, setKeep] = useState({});      // hash -> id to keep
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState(null);
  const [plans, setPlans] = useState(null);
  const [preview, setPreview] = useState(null);   // the confirm sheet
  const [showAll, setShowAll] = useState(false);  // the long per-group list

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [d, p] = await Promise.all([
        apiCall('/api/duplicates'),
        apiCall('/api/duplicates/plans').catch(() => null),
      ]);
      setData(d);
      setPlans(p);
      const initial = {};
      for (const g of d.groups) initial[g.hash] = g.files[0]?.id;
      setKeep(initial);
    } catch {
      setData({ groups: [], group_count: 0, reclaimable_formatted: '0 B' });
    }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  /** Ask the server what a rule WOULD do. Nothing is deleted here. */
  const previewRule = async (body, label) => {
    setBusy(true);
    try {
      const r = await apiCall('/api/duplicates/preview', {
        method: 'POST', body: JSON.stringify(body),
      });
      setPreview({ ...r, label, body });
    } catch (e) {
      setToast(e.message || 'Could not work that out');
      setTimeout(() => setToast(null), 4000);
    }
    setBusy(false);
  };

  /** Apply what the preview showed. */
  const applyPreview = async () => {
    if (!preview) return;
    setBusy(true);
    try {
      const ids = preview.files.map((f) => f.id);
      const r = await apiCall('/api/duplicates/resolve', {
        method: 'POST', body: JSON.stringify({ ids }),
      });
      setPreview(null);
      setToast(`Moved ${r.deleted} files to the trash — freed ${r.freed_formatted}`
               + (r.refused?.length ? ` (${r.refused.length} skipped for safety)` : ''));
      setTimeout(() => setToast(null), 7000);
      await load();
    } catch (e) {
      setToast(e.message || 'That did not work');
      setTimeout(() => setToast(null), 5000);
    }
    setBusy(false);
  };

  const deleteGroup = async (group) => {
    const keepId = keep[group.hash];
    const doomed = group.files.filter((f) => f.id !== keepId);
    if (!doomed.length) return;

    const msg =
      `Delete ${doomed.length} duplicate${doomed.length === 1 ? '' : 's'} ` +
      `and free ${group.reclaimable_formatted}?\n\n` +
      `KEEPING:\n  ${group.files.find((f) => f.id === keepId)?.filepath}\n\n` +
      `DELETING:\n${doomed.map((f) => '  ' + f.filepath).join('\n')}\n\n` +
      `These go to the trash, so you can still get them back.`;
    if (!window.confirm(msg)) return;

    setBusy(true);
    try {
      for (const f of doomed) {
        await apiCall(`/api/videos/${f.id}?delete_file=true`, { method: 'DELETE' });
      }
      setToast(`Freed ${group.reclaimable_formatted}`);
      setTimeout(() => setToast(null), 4000);
      await load();
    } catch (e) {
      setToast(`Failed: ${e.message || e}`);
      setTimeout(() => setToast(null), 5000);
    }
    setBusy(false);
  };

  const rescan = async () => {
    setBusy(true);
    try {
      await apiCall('/api/duplicates/scan', { method: 'POST', body: JSON.stringify({}) });
      setToast('Scanning in the background — refresh in a minute.');
      setTimeout(() => setToast(null), 5000);
    } catch { /* admin only */ }
    setBusy(false);
  };

  return (
    <div className="h-full overflow-y-auto px-6 py-6">
      <div className="mx-auto max-w-5xl">
        <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-medium text-zinc-100">Duplicates</h1>
            <p className="mt-1 text-sm text-zinc-500">
              Matched on file content, not filename — so renamed copies are still caught.
            </p>
          </div>
          <div className="flex gap-2">
            <button onClick={rescan} disabled={busy}
                    className="flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50">
              <RefreshCw className="h-4 w-4" /> Rescan
            </button>
            <button onClick={load}
                    className="flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800">
              Refresh
            </button>
          </div>
        </div>

        {loading ? (
          <p className="text-zinc-500">Loading…</p>
        ) : !data?.groups?.length ? (
          <div className="rounded-xl border border-dashed border-zinc-800 py-16 text-center">
            <ShieldCheck className="mx-auto mb-3 h-8 w-8 text-emerald-600" />
            <p className="text-zinc-400">No duplicates found.</p>
          </div>
        ) : (
          <>
            <div className="mb-5 flex items-center gap-3 rounded-xl border border-accent/30 bg-accent/10 px-4 py-3">
              <HardDrive className="h-5 w-5 shrink-0 text-accent" />
              <p className="text-sm text-zinc-200">
                <strong className="font-mono">{data.reclaimable_formatted}</strong> can be reclaimed
                across <strong>{data.group_count}</strong> groups.
                {data.unhashed > 0 && (
                  <span className="ml-2 text-zinc-400">({data.unhashed} files not yet hashed)</span>
                )}
              </p>
            </div>

            {/* ---- Quick cleanup: decide by folder, not file by file ---- */}
            {plans?.plans?.length > 0 && (
              <div className="mb-6 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
                <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold text-zinc-100">
                  <Zap className="h-4 w-4 text-accent" /> Quick cleanup
                </h2>
                <p className="mb-4 text-xs leading-relaxed text-zinc-500">
                  Your duplicates aren't random — the same folders copy each other over and over.
                  Pick which side to keep and it resolves every group at once.
                  You'll see exactly what will go before anything happens.
                </p>

                <div className="space-y-2">
                  {plans.plans.filter((p) => !p.single_folder).slice(0, 8).map((p) => (
                    <div key={p.key} className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
                      <div className="mb-2 flex flex-wrap items-center gap-2 font-mono text-[11px] text-zinc-500">
                        <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-zinc-300">{p.groups} groups</span>
                        <span className="text-accent">{p.reclaimable_formatted}</span>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        {p.folders.map((f, i) => (
                          <div key={f} className="flex items-center gap-2">
                            {i > 0 && <span className="text-xs text-zinc-600">vs</span>}
                            <button
                              disabled={busy}
                              onClick={() => previewRule(
                                { rule: 'prefer_folder', keep_folder: f, hashes: p.hashes },
                                `Keep everything in "${f}"`
                              )}
                              className="rounded border border-zinc-700 px-2.5 py-1.5 text-left text-xs text-zinc-300 transition hover:border-emerald-600/60 hover:bg-emerald-600/10 hover:text-emerald-300 disabled:opacity-40"
                              title={`Keep the copies in ${f}, trash the rest`}
                            >
                              keep <span className="font-mono">{f}</span>
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>

                <div className="mt-3 border-t border-zinc-800 pt-3">
                  <button
                    disabled={busy}
                    onClick={() => previewRule({ rule: 'drop_numbered' },
                      'Delete every "(2)" copy where the original still exists')}
                    className="flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-300 transition hover:bg-zinc-800 disabled:opacity-40"
                  >
                    <Copy className="h-3.5 w-3.5" />
                    Delete every “(2)” copy where the original is still there
                  </button>
                  <p className="mt-1.5 text-[11px] text-zinc-600">
                    Only touches groups that have both a clean name and a numbered copy.
                    Groups where every copy is numbered are left alone.
                  </p>
                </div>
              </div>
            )}

            {/* ---- the full per-group list, collapsed by default ---- */}
            <button
              onClick={() => setShowAll((v) => !v)}
              className="mb-3 flex items-center gap-2 text-sm text-zinc-400 hover:text-zinc-100"
            >
              {showAll ? <ChevronDown className="h-4 w-4" /> : <ChevRight className="h-4 w-4" />}
              Review all {data.group_count} groups one by one
            </button>

            <div className={'space-y-4 ' + (showAll ? '' : 'hidden')}>
              {data.groups.map((g) => (
                <div key={g.hash} className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <span className="flex items-center gap-2 font-mono text-xs text-zinc-400">
                      <Copy className="h-3.5 w-3.5" />
                      {g.files.length} copies · {g.size_formatted} each ·
                      <span className="text-accent">reclaim {g.reclaimable_formatted}</span>
                      {!g.verified && (
                        <span className="flex items-center gap-1 text-amber-500" title="Matched on a partial signature">
                          <AlertTriangle className="h-3 w-3" /> quick match
                        </span>
                      )}
                    </span>
                    <button
                      onClick={() => deleteGroup(g)}
                      disabled={busy}
                      className="flex items-center gap-1.5 rounded-lg bg-red-600/90 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-red-500 disabled:opacity-50"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      Delete the other {g.files.length - 1}
                    </button>
                  </div>

                  <ul className="space-y-1.5">
                    {g.files.map((f) => (
                      <li key={f.id}>
                        <label className={
                          'flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 transition ' +
                          (keep[g.hash] === f.id
                            ? 'border-emerald-600/50 bg-emerald-600/10'
                            : 'border-zinc-800 hover:border-zinc-700')
                        }>
                          <input
                            type="radio"
                            name={`keep-${g.hash}`}
                            checked={keep[g.hash] === f.id}
                            onChange={() => setKeep((k) => ({ ...k, [g.hash]: f.id }))}
                            className="h-4 w-4 accent-emerald-500"
                          />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm text-zinc-200">{f.filename}</span>
                            <span className="block truncate font-mono text-[11px] text-zinc-500">{f.folder}</span>
                          </span>
                          <span className={
                            'shrink-0 rounded px-2 py-0.5 text-[10px] font-medium ' +
                            (keep[g.hash] === f.id
                              ? 'bg-emerald-600/20 text-emerald-400'
                              : 'bg-red-600/15 text-red-400')
                          }>
                            {keep[g.hash] === f.id ? 'KEEP' : 'delete'}
                          </span>
                        </label>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {preview && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/70 p-4"
             onClick={() => setPreview(null)}>
          <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-xl border border-zinc-700 bg-zinc-900"
               onClick={(e) => e.stopPropagation()}>
            <div className="border-b border-zinc-800 px-5 py-4">
              <h2 className="text-base font-medium text-zinc-100">{preview.label}</h2>
              <p className="mt-1 text-sm text-zinc-400">
                <strong className="font-mono text-accent">{preview.delete_count} files</strong> would go to the trash,
                freeing <strong className="font-mono">{preview.reclaimable_formatted}</strong>.
                One copy of every clip is kept.
                {preview.skipped > 0 && (
                  <span className="mt-1 block text-xs text-zinc-500">
                    {preview.skipped} groups left alone — this rule couldn't decide them safely.
                  </span>
                )}
              </p>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-3">
              <ul className="space-y-1.5">
                {preview.files.map((f) => (
                  <li key={f.id} className="rounded border border-zinc-800 px-3 py-2 text-xs">
                    <div className="flex items-center gap-2 text-red-400">
                      <Trash2 className="h-3 w-3 shrink-0" />
                      <span className="truncate">{f.filename}</span>
                    </div>
                    <div className="mt-0.5 truncate pl-5 font-mono text-[10px] text-zinc-600">{f.folder}</div>
                    <div className="mt-1 flex items-center gap-2 pl-5 text-[10px] text-emerald-500">
                      <ArrowRight className="h-3 w-3 shrink-0" />
                      keeping <span className="font-mono">{f.keeping_folder}/{f.keeping}</span>
                    </div>
                  </li>
                ))}
              </ul>
              {preview.truncated && (
                <p className="mt-3 text-xs text-zinc-500">…and more. All of them will be trashed.</p>
              )}
            </div>

            <div className="flex items-center gap-2 border-t border-zinc-800 px-5 py-4">
              <button
                onClick={applyPreview}
                disabled={busy || preview.delete_count === 0}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-red-500 disabled:opacity-40"
              >
                {busy ? 'Working…' : `Move ${preview.delete_count} files to trash`}
              </button>
              <button onClick={() => setPreview(null)}
                      className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
                Cancel
              </button>
              <span className="ml-auto text-[11px] text-zinc-600">
                Recoverable from Trash until you empty it
              </span>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div className="fixed bottom-4 left-1/2 z-[100] -translate-x-1/2 rounded-full bg-zinc-800 px-4 py-2 text-sm text-zinc-100 shadow-lg">
          {toast}
        </div>
      )}
    </div>
  );
}
