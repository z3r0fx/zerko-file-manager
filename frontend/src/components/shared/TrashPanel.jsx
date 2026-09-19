import { useEffect, useState, useCallback } from 'react';
import { X, Trash2, RotateCcw, AlertTriangle, HardDrive } from 'lucide-react';
import { apiCall, API_BASE } from '../../lib/api';

export default function TrashPanel({ open, onClose, onChanged }) {
  const [data, setData] = useState({ count: 0, total_size: 0, items: [] });
  const [busy, setBusy] = useState(false);
  const [confirmEmpty, setConfirmEmpty] = useState(false);

  const load = useCallback(async () => {
    try { setData(await apiCall('/api/trash')); } catch { /* keep what we have */ }
  }, []);

  useEffect(() => { if (open) load(); }, [open, load]);
  if (!open) return null;

  const restore = async (id) => {
    setBusy(true);
    try { await apiCall(`/api/videos/${id}/restore`, { method: 'POST' }); await load(); onChanged?.(); }
    finally { setBusy(false); }
  };

  const empty = async () => {
    setBusy(true);
    try {
      const r = await apiCall('/api/trash/empty', { method: 'POST' });
      await load(); onChanged?.();
      setConfirmEmpty(false);
      alert(`Freed ${r.freed_formatted} by permanently deleting ${r.removed} file(s).`);
    } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/75 backdrop-blur-sm animate-shade-in" onClick={onClose} />
      <div className="relative w-full max-w-3xl max-h-[85vh] flex flex-col bg-zinc-900 border border-zinc-700 rounded-xl shadow-2xl animate-pop-in overflow-hidden">

        <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-800">
          <div className="flex items-center gap-3">
            <Trash2 className="w-5 h-5 text-zinc-400" />
            <h2 className="text-base font-semibold text-zinc-100">Trash</h2>
            <span className="text-xs font-mono text-zinc-500">{data.count} file(s)</span>
          </div>
          <button type="button" onClick={onClose} className="text-zinc-500 hover:text-zinc-100 transition" aria-label="Close">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-5 py-4 border-b border-zinc-800 flex items-center gap-4">
          <div className="flex items-center gap-2.5 flex-1">
            <HardDrive className="w-4 h-4 text-[#ff5c1f]" />
            <div>
              <div className="text-lg font-semibold text-zinc-100 leading-none">
                {data.total_size_formatted || '0 B'}
              </div>
              <div className="text-[11px] text-zinc-500 mt-1">
                reclaimable — these files still take up space until you empty the trash
              </div>
            </div>
          </div>
          {data.count > 0 && (
            <button type="button" disabled={busy} onClick={() => setConfirmEmpty(true)}
                    className="px-3.5 py-2 rounded-lg bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white text-sm font-medium transition">
              Empty trash
            </button>
          )}
        </div>

        {confirmEmpty && (
          <div className="px-5 py-3 bg-red-950/40 border-b border-red-900/60 flex items-center gap-3">
            <AlertTriangle className="w-4 h-4 text-red-400 shrink-0" />
            <span className="text-xs text-red-200 flex-1">
              Permanently delete {data.count} file(s) and free {data.total_size_formatted}? This cannot be undone.
            </span>
            <button type="button" onClick={() => setConfirmEmpty(false)} className="text-xs text-zinc-400 hover:text-zinc-200 px-2">Cancel</button>
            <button type="button" disabled={busy} onClick={empty}
                    className="text-xs bg-red-600 hover:bg-red-500 text-white px-3 py-1.5 rounded transition disabled:opacity-50">
              Yes, delete
            </button>
          </div>
        )}

        <div className="flex-1 overflow-y-auto">
          {data.items.length === 0 ? (
            <p className="px-5 py-10 text-center text-sm text-zinc-500">
              Trash is empty. Deleted clips land here first, so nothing is lost by mistake.
            </p>
          ) : data.items.map((it) => (
            <div key={it.id} className="flex items-center gap-3 px-5 py-2.5 border-b border-zinc-800/60 last:border-0">
              <div className="w-16 h-10 shrink-0 bg-black rounded overflow-hidden">
                {it.thumbnail_path && (
                  <img src={`${API_BASE}${it.thumbnail_path}`} alt="" loading="lazy" className="w-full h-full object-cover" />
                )}
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-sm text-zinc-200 truncate">{it.filename}</div>
                <div className="text-[11px] font-mono text-zinc-600 truncate" title={it.original_path}>
                  {it.original_path}
                </div>
              </div>
              <span className="text-xs font-mono text-zinc-400 shrink-0">{it.file_size_formatted}</span>
              <button type="button" disabled={busy} onClick={() => restore(it.id)}
                      className="flex items-center gap-1.5 text-xs text-zinc-400 hover:text-zinc-100 transition shrink-0 disabled:opacity-50">
                <RotateCcw className="w-3.5 h-3.5" /> Restore
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
