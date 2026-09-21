import { useRef, useState } from 'react';
import { Upload, Check, AlertCircle, X, Loader2, Image as ImageIcon } from 'lucide-react';

/**
 * The upload half of a share link, as it appears on a phone.
 *
 * Deliberately plain: one big button that opens the camera roll, a list of
 * what is going up, and nothing else. Files are sent one at a time rather
 * than in one request - a phone on mobile data drops connections, and losing
 * one clip of twelve is recoverable where losing the whole batch is not.
 */
export default function PhoneUpload({ token, password, folderName, onDone }) {
  const inputRef = useRef(null);
  const [items, setItems] = useState([]);   // {name, size, state, error}
  const [busy, setBusy] = useState(false);
  const [sender, setSender] = useState(() => {
    try { return localStorage.getItem('share_viewer_name') || ''; } catch { return ''; }
  });

  const pick = () => inputRef.current?.click();

  const send = async (files) => {
    const list = Array.from(files || []);
    if (!list.length) return;

    setItems((prev) => [
      ...prev,
      ...list.map((f) => ({ name: f.name, size: f.size, state: 'waiting' })),
    ]);
    setBusy(true);

    const base = items.length;
    for (let i = 0; i < list.length; i += 1) {
      const f = list[i];
      const at = base + i;
      setItems((prev) => prev.map((it, n) => (n === at ? { ...it, state: 'sending' } : it)));
      try {
        const body = new FormData();
        body.append('file', f);
        if (password) body.append('password', password);
        if (sender.trim()) body.append('sender', sender.trim());
        const res = await fetch(`/api/public/share/${token}/upload`, { method: 'POST', body });
        if (!res.ok) {
          const j = await res.json().catch(() => ({}));
          throw new Error(j.detail || `Upload failed (${res.status})`);
        }
        setItems((prev) => prev.map((it, n) => (n === at ? { ...it, state: 'done' } : it)));
      } catch (e) {
        setItems((prev) => prev.map((it, n) => (
          n === at ? { ...it, state: 'failed', error: e.message } : it)));
      }
    }

    setBusy(false);
    try { if (sender.trim()) localStorage.setItem('share_viewer_name', sender.trim()); } catch { /* private window */ }
    onDone?.();
  };

  const done = items.filter((i) => i.state === 'done').length;
  const failed = items.filter((i) => i.state === 'failed').length;

  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.03] p-4">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold text-zinc-100">Send files in</h2>
        {folderName && (
          <span className="truncate text-[11px] text-zinc-500">into {folderName}</span>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => { send(e.target.files); e.target.value = ''; }}
      />

      <button
        onClick={pick}
        disabled={busy}
        className="flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-4 py-3.5 text-sm font-semibold text-accent-foreground transition disabled:opacity-60"
      >
        {busy ? <Loader2 className="h-5 w-5 animate-spin" /> : <ImageIcon className="h-5 w-5" />}
        {busy ? 'Uploading…' : 'Choose files'}
      </button>

      <input
        value={sender}
        onChange={(e) => setSender(e.target.value)}
        placeholder="Your name (so we know who sent it)"
        className="mt-2.5 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2.5 text-sm text-zinc-100 outline-none focus:border-accent"
      />

      {items.length > 0 && (
        <>
          <ul className="mt-3 max-h-56 space-y-1 overflow-y-auto">
            {items.map((it, n) => (
              <li key={`${it.name}-${n}`} className="flex items-center gap-2 text-xs">
                {it.state === 'done' && <Check className="h-3.5 w-3.5 shrink-0 text-emerald-400" />}
                {it.state === 'failed' && <AlertCircle className="h-3.5 w-3.5 shrink-0 text-red-400" />}
                {it.state === 'sending' && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />}
                {it.state === 'waiting' && <Upload className="h-3.5 w-3.5 shrink-0 text-zinc-600" />}
                <span className={`min-w-0 flex-1 truncate ${it.state === 'failed' ? 'text-red-300' : 'text-zinc-400'}`}>
                  {it.name}
                </span>
                {it.state === 'failed' && (
                  <span className="shrink-0 text-[10px] text-red-400/80">{it.error}</span>
                )}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[11px] text-zinc-500">
            {done} sent{failed ? `, ${failed} failed` : ''}. You can close this page once
            everything shows a tick.
          </p>
        </>
      )}
    </div>
  );
}
