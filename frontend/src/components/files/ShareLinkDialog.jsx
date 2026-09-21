import { useCallback, useEffect, useState } from 'react';
import { Link2, Copy, Check, Lock, Clock, Trash2, Loader2, Eye, Download, Globe, Plus } from 'lucide-react';
import { cn } from '../../lib/utils';
import { Modal, Button } from './Dialogs';
import { filesApi, formatWhen } from '../../lib/files';

const EXPIRY = [
  { label: 'Never', value: 0 },
  { label: '1 day', value: 1 },
  { label: '7 days', value: 7 },
  { label: '30 days', value: 30 },
  { label: '90 days', value: 90 },
];

const fullUrl = (s) => `${window.location.origin}${s.url_path}`;

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

function LinkRow({ s, onRevoke, onCopied }) {
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const copy = async () => {
    if (await copyText(fullUrl(s))) { setCopied(true); onCopied?.(); setTimeout(() => setCopied(false), 1800); }
  };
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/50 p-3">
      <div className="flex items-center gap-2">
        <input readOnly value={fullUrl(s)} onFocus={(e) => e.target.select()}
               className="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-300 outline-none focus:border-accent" />
        <Button variant={copied ? 'outline' : 'solid'} onClick={copy} className="shrink-0 !px-3">
          {copied ? <><Check className="h-4 w-4 text-emerald-400" /> Copied</> : <><Copy className="h-4 w-4" /> Copy</>}
        </Button>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-zinc-500">
        <span className="inline-flex items-center gap-1"><Lock className={cn('h-3 w-3', s.has_password && 'text-amber-400')} />{s.has_password ? 'Password protected' : 'Anyone with the link'}</span>
        <span className="inline-flex items-center gap-1"><Clock className="h-3 w-3" />{s.expires_at ? `Expires ${formatWhen(s.expires_at)}` : 'Never expires'}</span>
        <span className="inline-flex items-center gap-1"><Eye className="h-3 w-3" />{s.views} view{s.views === 1 ? '' : 's'}</span>
        <span className="inline-flex items-center gap-1"><Download className="h-3 w-3" />{s.downloads} download{s.downloads === 1 ? '' : 's'}</span>
        <button disabled={busy} onClick={async () => { setBusy(true); try { await onRevoke(s); } finally { setBusy(false); } }}
                className="ml-auto inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-red-400 hover:bg-red-500/10 disabled:opacity-50">
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />} Turn off
        </button>
      </div>
    </div>
  );
}

export default function ShareLinkDialog({ entry, onClose, onChanged, toast }) {
  const [links, setLinks] = useState(null);
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [usePassword, setUsePassword] = useState(false);
  const [password, setPassword] = useState('');
  const [expiry, setExpiry] = useState(0);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await filesApi.shares(entry.path);
      setLinks(r.shares);
      setShowForm(r.shares.length === 0);
    } catch (e) { setError(e.message); setLinks([]); }
  }, [entry.path]);

  useEffect(() => { setLinks(null); setError(''); setUsePassword(false); setPassword(''); setExpiry(0); load(); }, [load]);

  const create = async () => {
    setError('');
    if (usePassword && password.trim().length < 4) { setError('Use at least 4 characters for the password.'); return; }
    setBusy(true);
    try {
      const s = await filesApi.createShare(entry.path, usePassword ? password.trim() : null, expiry || null);
      setLinks((l) => [s, ...(l || [])]);
      setShowForm(false); setPassword(''); setUsePassword(false);
      onChanged?.();
      if (await copyText(fullUrl(s))) toast?.('Link created and copied', { tone: 'success' });
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  };

  const revoke = async (s) => {
    try {
      await filesApi.revokeShare(s.id);
      setLinks((l) => l.filter((x) => x.id !== s.id));
      onChanged?.();
      toast?.('Link turned off');
    } catch (e) { setError(e.message); }
  };

  const noun = entry.is_dir ? 'folder' : 'file';

  return (
    <Modal open onClose={onClose} title={`Share "${entry.name}"`} width="max-w-lg"
           footer={<Button onClick={onClose}>Done</Button>}>
      <p className="mb-4 flex items-start gap-2 text-sm text-zinc-400">
        <Globe className="mt-0.5 h-4 w-4 shrink-0 text-zinc-500" />
        <span>Anyone with a link can download this {noun}{entry.is_dir ? ' (everything inside it)' : ''} - no account needed. Turn a link off at any time.</span>
      </p>

      {links === null && <div className="grid place-items-center py-8"><Loader2 className="h-5 w-5 animate-spin text-zinc-500" /></div>}

      {links && links.length > 0 && (
        <div className="space-y-2.5">
          {links.map((s) => <LinkRow key={s.id} s={s} onRevoke={revoke} onCopied={() => toast?.('Link copied', { tone: 'success' })} />)}
        </div>
      )}

      {links && !showForm && (
        <button onClick={() => setShowForm(true)} className="mt-3 inline-flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100">
          <Plus className="h-4 w-4" /> Create another link
        </button>
      )}

      {links && showForm && (
        <div className={cn('rounded-xl border border-zinc-800 bg-zinc-950/50 p-4', links.length > 0 && 'mt-3')}>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-zinc-500">Expires</label>
              <select value={expiry} onChange={(e) => setExpiry(Number(e.target.value))}
                      className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-accent">
                {EXPIRY.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>
            <div>
              <label className="mb-1.5 flex cursor-pointer items-center gap-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
                <input type="checkbox" checked={usePassword} onChange={(e) => setUsePassword(e.target.checked)} className="accent-red-600" />
                Require a password
              </label>
              <input type="text" disabled={!usePassword} value={password} onChange={(e) => setPassword(e.target.value)}
                     placeholder={usePassword ? 'At least 4 characters' : 'Off'} autoComplete="off"
                     className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-accent disabled:opacity-40" />
            </div>
          </div>
          <div className="mt-4 flex items-center justify-end gap-2">
            {links.length > 0 && <Button onClick={() => setShowForm(false)}>Cancel</Button>}
            <Button variant="solid" onClick={create} disabled={busy}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />} Create link
            </Button>
          </div>
        </div>
      )}
      {error && <p className="mt-3 text-xs text-red-400">{error}</p>}
    </Modal>
  );
}
