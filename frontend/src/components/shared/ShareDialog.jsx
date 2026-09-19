import { useState } from 'react';
import { Link2, Copy, Check, X } from 'lucide-react';
import { apiCall } from '../../lib/api';

/**
 * Create a client link for a folder or the current selection.
 * Defaults are the safe ones: no downloads, picking on, 30-day expiry.
 */
export default function ShareDialog({ folderId, folderName, videoIds, onClose }) {
  const [title, setTitle] = useState(folderName ? `${folderName}` : 'Media for review');
  const [message, setMessage] = useState('');
  const [expiresDays, setExpiresDays] = useState(30);
  const [password, setPassword] = useState('');
  const [allowDownload, setAllowDownload] = useState(false);
  const [allowSelects, setAllowSelects] = useState(true);
  const [includeSubfolders, setIncludeSubfolders] = useState(true);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);

  const count = videoIds?.length || 0;

  const create = async () => {
    setBusy(true); setError(null);
    try {
      const body = {
        title, message,
        expires_days: expiresDays ? Number(expiresDays) : null,
        password: password || undefined,
        allow_download: allowDownload,
        allow_selects: allowSelects,
      };
      if (count > 0) body.video_ids = videoIds;
      else { body.folder_id = folderId; body.include_subfolders = includeSubfolders; }

      const res = await apiCall('/api/shares', { method: 'POST', body: JSON.stringify(body) });
      setResult({ ...res, fullUrl: `${window.location.origin}${res.url}` });
    } catch (e) {
      setError(e.message || 'Could not create the link');
    }
    setBusy(false);
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(result.fullUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = result.fullUrl; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); document.body.removeChild(ta);
      setCopied(true);
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 px-5 py-4">
          <h2 className="flex items-center gap-2 text-base font-medium text-zinc-100">
            <Link2 className="h-4 w-4 text-[#ff5c1f]" />
            {result ? 'Link ready' : 'Share with a client'}
          </h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-200"><X className="h-4 w-4" /></button>
        </div>

        {result ? (
          <div className="space-y-4 px-5 py-5">
            <p className="text-sm text-zinc-400">
              {result.count} {result.count === 1 ? 'clip' : 'clips'}. Anyone with this link can view
              {allowDownload ? ' and download' : ''} — no account needed.
            </p>
            <div className="flex gap-2">
              <input
                readOnly
                value={result.fullUrl}
                onFocus={(e) => e.target.select()}
                className="flex-1 rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-xs text-zinc-300"
              />
              <button
                onClick={copy}
                className="flex items-center gap-2 rounded-lg bg-[#ff5c1f] px-4 py-2 text-sm font-medium text-black transition hover:bg-[#ff7a45]"
              >
                {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <button onClick={onClose} className="w-full rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
              Done
            </button>
          </div>
        ) : (
          <div className="space-y-4 px-5 py-5">
            <p className="text-sm text-zinc-400">
              {count > 0
                ? `Sharing ${count} selected ${count === 1 ? 'clip' : 'clips'}.`
                : `Sharing the folder "${folderName}".`}
            </p>

            <Field label="Title">
              <input value={title} onChange={(e) => setTitle(e.target.value)} className={inputCls} />
            </Field>

            <Field label="Note for the viewer (optional)">
              <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={2}
                        placeholder="Pick the ones you want and I'll cut them together."
                        className={inputCls} />
            </Field>

            <div className="flex gap-3">
              <Field label="Expires after" className="flex-1">
                <select value={expiresDays} onChange={(e) => setExpiresDays(e.target.value)} className={inputCls}>
                  <option value="7">7 days</option>
                  <option value="30">30 days</option>
                  <option value="90">90 days</option>
                  <option value="">Never</option>
                </select>
              </Field>
              <Field label="Password (optional)" className="flex-1">
                <input type="text" value={password} onChange={(e) => setPassword(e.target.value)}
                       placeholder="leave blank for none" className={inputCls} />
              </Field>
            </div>

            <div className="space-y-2 rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
              <Toggle checked={allowSelects} onChange={setAllowSelects}
                      label="Let them pick favourites"
                      hint="Their picks come back to you, ready to export into Resolve." />
              <Toggle checked={allowDownload} onChange={setAllowDownload}
                      label="Allow downloads"
                      hint="Off means view-only streaming of the proxy." />
              {count === 0 && (
                <Toggle checked={includeSubfolders} onChange={setIncludeSubfolders}
                        label="Include subfolders" />
              )}
            </div>

            {error && <p className="text-sm text-red-400">{error}</p>}

            <div className="flex gap-2">
              <button
                onClick={create}
                disabled={busy}
                className="flex-1 rounded-lg bg-[#ff5c1f] px-4 py-2 text-sm font-medium text-black transition hover:bg-[#ff7a45] disabled:opacity-50"
              >
                {busy ? 'Creating…' : 'Create link'}
              </button>
              <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const inputCls =
  'w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-[#ff5c1f]';

function Field({ label, children, className = '' }) {
  return (
    <label className={`block ${className}`}>
      <span className="mb-1 block text-xs text-zinc-500">{label}</span>
      {children}
    </label>
  );
}

function Toggle({ checked, onChange, label, hint }) {
  return (
    <label className="flex cursor-pointer items-start gap-3">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)}
             className="mt-0.5 h-4 w-4 accent-[#ff5c1f]" />
      <span>
        <span className="block text-sm text-zinc-200">{label}</span>
        {hint && <span className="block text-xs text-zinc-500">{hint}</span>}
      </span>
    </label>
  );
}
