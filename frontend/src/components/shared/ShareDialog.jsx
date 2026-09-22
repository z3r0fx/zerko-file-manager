import { useEffect, useState } from 'react';
import { Link2, Copy, Check, X, Smartphone, Send, Inbox } from 'lucide-react';
import { apiCall } from '../../lib/api';
import QRCode from 'qrcode';

/**
 * Create a portal: a link that lets someone view, download and send in
 * files, without an account. The same token machinery either way - a
 * portal that only shows things is what used to be called a client link.
 * Defaults are the safe ones: no downloads, picking on, 30-day expiry.
 */
export default function ShareDialog({ folderId, folderName, videoIds, onClose }) {
  const [title, setTitle] = useState(folderName || 'Media for review');
  const [message, setMessage] = useState('');
  const [expiresDays, setExpiresDays] = useState(30);
  const [password, setPassword] = useState('');
  const [allowDownload, setAllowDownload] = useState(false);
  const [allowSelects, setAllowSelects] = useState(true);
  // With no folder and no selection there is nothing to show, so the only
  // thing such a portal can be is one that receives.
  // A portal is one of two things, and saying so up front is clearer than a
  // row of flags that might add up to either.
  const hasContent = !!folderId || !!(videoIds?.length);
  const [kind, setKind] = useState(hasContent ? 'send' : 'receive');
  const allowUpload = kind !== 'send';
  const [allowZip, setAllowZip] = useState(true);
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
        kind,
        allow_upload: allowUpload,
        allow_zip: allowZip,
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
            <Link2 className="h-4 w-4 text-accent" />
            {result ? 'Portal ready' : 'Create a portal'}
          </h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-200"><X className="h-4 w-4" /></button>
        </div>

        {result ? (
          <div className="space-y-4 px-5 py-5">
            <p className="text-sm text-zinc-400">
              {allowUpload && result.count === 0
                ? 'Anyone with this link can send files in. They land in an inbox for you to file — no account needed.'
                : `${result.count} ${result.count === 1 ? 'clip' : 'clips'}. Anyone with this link can view${allowDownload ? ' and download' : ''}${allowUpload ? ', and send files back' : ''} — no account needed.`}
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
                className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition hover:bg-accent-hi"
              >
                {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <ScanToOpen url={result.fullUrl} upload={allowUpload} />

            <button onClick={onClose} className="w-full rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
              Done
            </button>
          </div>
        ) : (
          <div className="space-y-4 px-5 py-5">
            <p className="text-sm text-zinc-400">
              {count > 0
                ? `A portal showing ${count} selected ${count === 1 ? 'clip' : 'clips'}.`
                : folderName
                  ? `A portal onto the folder "${folderName}".`
                  : 'A portal for receiving files. It starts empty and fills up from whoever you send it to.'}
            </p>

            <div className="grid grid-cols-2 gap-2">
              <KindCard
                icon={Send} title="Sending" active={kind === 'send'}
                disabled={!hasContent}
                onClick={() => setKind('send')}
                hint="They look, pick favourites and leave notes. Downloads optional."
              />
              <KindCard
                icon={Inbox} title="Receiving" active={kind === 'receive'}
                onClick={() => setKind('receive')}
                hint="They send files in and see nothing of your library."
              />
            </div>

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

            {kind !== 'receive' && (
            <div className="space-y-2 rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
              <Toggle checked={allowSelects} onChange={setAllowSelects}
                      label="Let them pick favourites"
                      hint="Their picks come back to you, ready to export into Resolve." />
              <Toggle checked={allowDownload} onChange={setAllowDownload}
                      label="Allow downloads"
                      hint="Off means view-only streaming of the proxy." />
              {allowDownload && (
                <Toggle checked={allowZip} onChange={setAllowZip}
                        label="Offer the whole folder as one zip"
                        hint="Off means files one at a time. That is what a phone wants anyway - a zip lands in Files, not the camera roll - so this is ignored on phones either way." />
              )}

              {count === 0 && (
                <Toggle checked={includeSubfolders} onChange={setIncludeSubfolders}
                        label="Include subfolders" />
              )}
            </div>
            )}

            {error && <p className="text-sm text-red-400">{error}</p>}

            <div className="flex gap-2">
              <button
                onClick={create}
                disabled={busy}
                className="flex-1 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition hover:bg-accent-hi disabled:opacity-50"
              >
                {busy ? 'Creating…' : 'Create portal'}
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
  'w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-accent';

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
             className="mt-0.5 h-4 w-4 accent-accent" />
      <span>
        <span className="block text-sm text-zinc-200">{label}</span>
        {hint && <span className="block text-xs text-zinc-500">{hint}</span>}
      </span>
    </label>
  );
}

/**
 * A QR code for the link, so the phone in your hand can open it without
 * anyone typing a 43-character token. Rendered locally - the URL is never
 * sent to a QR service, which would hand a third party a working link to
 * the footage.
 */
function ScanToOpen({ url, upload }) {
  const [src, setSrc] = useState(null);

  useEffect(() => {
    let alive = true;
    QRCode.toDataURL(url, { margin: 1, width: 320, color: { dark: '#0b0b0d', light: '#ffffff' } })
      .then((d) => { if (alive) setSrc(d); })
      .catch(() => { if (alive) setSrc(null); });
    return () => { alive = false; };
  }, [url]);

  if (!src) return null;

  return (
    <div className="flex items-center gap-4 rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
      <img src={src} alt="QR code for this link" className="h-24 w-24 shrink-0 rounded bg-white p-1" />
      <div className="min-w-0">
        <p className="flex items-center gap-1.5 text-sm font-medium text-zinc-200">
          <Smartphone className="h-4 w-4 text-accent" /> Scan to open on a phone
        </p>
        <p className="mt-1 text-xs leading-relaxed text-zinc-500">
          {upload
            ? 'Point your camera at this and the phone opens straight on the upload screen - pick photos or video and they land in the folder.'
            : 'Point a camera at this to open the link on a phone. Works for your client too, if you are showing them in person.'}
        </p>
      </div>
    </div>
  );
}

function KindCard({ icon: Icon, title, hint, active, disabled, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={
        'rounded-lg border p-3 text-left transition disabled:cursor-not-allowed disabled:opacity-40 ' +
        (active ? 'border-accent bg-accent/10' : 'border-zinc-700 hover:border-zinc-500')
      }
    >
      <span className={'flex items-center gap-2 text-sm font-medium ' +
                       (active ? 'text-accent' : 'text-zinc-200')}>
        <Icon className="h-4 w-4" /> {title}
      </span>
      <span className="mt-1 block text-[11px] leading-snug text-zinc-500">{hint}</span>
    </button>
  );
}
