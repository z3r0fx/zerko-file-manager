import { useCallback, useEffect, useState } from 'react';
import { X, FolderInput, FolderPlus, Loader2, CheckSquare, Square, Inbox } from 'lucide-react';
import { apiCall } from '../../lib/api';

const fmtSize = (b) => {
  if (!b) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; let n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};

/**
 * Everything a link has received, waiting to be filed.
 *
 * Uploads from a phone land in a holding folder rather than in the library,
 * so the decision about where footage belongs is made here, at a desk, once -
 * instead of by whoever happened to be holding the phone.
 */
export default function IntakePanel({ share, onClose, onFiled }) {
  const [data, setData] = useState(null);
  const [folders, setFolders] = useState([]);
  const [selected, setSelected] = useState(() => new Set());
  const [target, setTarget] = useState('');
  const [newFolder, setNewFolder] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [done, setDone] = useState(null);

  const load = useCallback(async () => {
    try {
      const [intake, tree] = await Promise.all([
        apiCall(`/api/shares/${share.id}/intake`),
        apiCall('/api/folders'),
      ]);
      setData(intake);
      setFolders(Array.isArray(tree) ? tree : (tree?.folders || []));
      setSelected(new Set());
    } catch (e) {
      setError(e.message || 'Could not load what has arrived');
    }
  }, [share.id]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const items = data?.items || [];
  const allSelected = items.length > 0 && selected.size === items.length;

  const toggle = (id) => setSelected((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const fileThem = async () => {
    setBusy(true); setError(null); setDone(null);
    try {
      const body = {};
      if (selected.size) body.video_ids = [...selected];
      if (newFolder.trim()) body.new_folder = newFolder.trim();
      else if (target) body.folder_id = Number(target);
      else throw new Error('Choose a folder, or name a new project');

      const res = await apiCall(`/api/shares/${share.id}/intake/file`, {
        method: 'POST', body: JSON.stringify(body),
      });
      setDone(`Moved ${res.moved} ${res.moved === 1 ? 'file' : 'files'} into ${res.folder}.`);
      setNewFolder('');
      await load();
      onFiled?.();
    } catch (e) {
      setError(e.message || 'Could not move those files');
    }
    setBusy(false);
  };

  return (
    <div
      className="animate-shade-in fixed inset-0 z-[70] flex items-center justify-center bg-black/70 p-0 sm:p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="animate-pop-in flex h-full w-full flex-col border-zinc-700 bg-zinc-900 sm:h-auto sm:max-h-[88vh] sm:w-full sm:max-w-3xl sm:rounded-xl sm:border sm:shadow-2xl">
        <div className="flex items-center justify-between border-b border-zinc-800 px-5 py-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-base font-medium text-zinc-100">
              <Inbox className="h-4 w-4 text-accent" />
              {share.title || 'Upload link'}
            </h2>
            <p className="mt-0.5 truncate text-xs text-zinc-500">
              {items.length === 0
                ? 'Nothing waiting. Anything sent through this link appears here.'
                : `${items.length} ${items.length === 1 ? 'file' : 'files'} waiting in ${data?.folder}`}
            </p>
          </div>
          <button onClick={onClose} className="rounded-md p-2 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {items.length === 0 ? (
            <p className="py-12 text-center text-sm text-zinc-500">
              Send the link or QR code to a phone and the photos and clips land here.
            </p>
          ) : (
            <>
              <button
                onClick={() => setSelected(allSelected ? new Set() : new Set(items.map((i) => i.id)))}
                className="mb-3 flex items-center gap-2 text-xs text-zinc-400 hover:text-zinc-100"
              >
                {allSelected ? <CheckSquare className="h-3.5 w-3.5" /> : <Square className="h-3.5 w-3.5" />}
                {allSelected ? 'Clear selection' : 'Select all'}
              </button>

              <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
                {items.map((v) => (
                  <button
                    key={v.id}
                    onClick={() => toggle(v.id)}
                    className={
                      'group relative overflow-hidden rounded-lg border bg-black/40 text-left transition ' +
                      (selected.has(v.id) ? 'border-accent ring-2 ring-accent/50' : 'border-zinc-800 hover:border-zinc-600')
                    }
                  >
                    <div className="aspect-video w-full bg-zinc-950">
                      {v.thumbnail_path ? (
                        <img src={v.thumbnail_path} alt="" loading="lazy"
                             className="h-full w-full object-cover" />
                      ) : (
                        <div className="grid h-full place-items-center text-[10px] uppercase tracking-wide text-zinc-600">
                          {v.media_type}
                        </div>
                      )}
                    </div>
                    <div className="px-2 py-1.5">
                      <p className="truncate text-[11px] text-zinc-300">{v.filename}</p>
                      <p className="truncate text-[10px] text-zinc-600">
                        {fmtSize(v.file_size)}{v.uploaded_by ? ` · ${v.uploaded_by}` : ''}
                      </p>
                    </div>
                    <span className={
                      'absolute left-1.5 top-1.5 rounded p-1 ' +
                      (selected.has(v.id) ? 'bg-accent text-accent-foreground' : 'bg-black/60 text-white/60')
                    }>
                      {selected.has(v.id) ? <CheckSquare className="h-3.5 w-3.5" /> : <Square className="h-3.5 w-3.5" />}
                    </span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        {items.length > 0 && (
          <div className="shrink-0 space-y-3 border-t border-zinc-800 px-5 py-4">
            <p className="text-xs text-zinc-500">
              {selected.size > 0
                ? `Filing ${selected.size} selected.`
                : `Filing all ${items.length}. Tick some to move only those.`}
            </p>
            <div className="flex flex-col gap-2 sm:flex-row">
              <label className="flex-1">
                <span className="mb-1 block text-xs text-zinc-500">Move into an existing folder</span>
                <select
                  value={target}
                  onChange={(e) => { setTarget(e.target.value); setNewFolder(''); }}
                  className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-accent"
                >
                  <option value="">Choose…</option>
                  {folders.map((f) => (
                    <option key={f.id} value={f.id}>{f.name}</option>
                  ))}
                </select>
              </label>
              <label className="flex-1">
                <span className="mb-1 block text-xs text-zinc-500">…or start a new project</span>
                <input
                  value={newFolder}
                  onChange={(e) => { setNewFolder(e.target.value); if (e.target.value) setTarget(''); }}
                  placeholder="e.g. 14 Ocean View"
                  className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-accent"
                />
              </label>
            </div>

            {error && <p className="text-sm text-red-400">{error}</p>}
            {done && <p className="text-sm text-emerald-400">{done}</p>}

            <button
              onClick={fileThem}
              disabled={busy || (!target && !newFolder.trim())}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-accent-foreground transition hover:bg-accent-hi disabled:opacity-40"
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" />
                    : newFolder.trim() ? <FolderPlus className="h-4 w-4" />
                    : <FolderInput className="h-4 w-4" />}
              {busy ? 'Moving…'
                    : newFolder.trim() ? `Create "${newFolder.trim()}" and move`
                    : 'Move into folder'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
