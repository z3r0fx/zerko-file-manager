import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { createPortal } from 'react-dom';
import {
  Download, Lock, Loader2, ChevronRight, Clock, FolderArchive, X, AlertCircle, ShieldCheck, Eye,
} from 'lucide-react';
import { cn } from '../lib/utils';
import {
  formatBytes, formatWhen, kindLabel, sortEntries, startDownload, canPlayVideo, canPlayAudio, isNativeImage,
} from '../lib/files';
import Thumb from '../components/files/Thumb';
import { KindGlyph, KindTile } from '../components/files/FileIcon';

const base = (token) => `/api/public/files/${encodeURIComponent(token)}`;
const store = (token) => `zerko.share.${token}`;
const readKey = (token) => { try { return sessionStorage.getItem(store(token)) || ''; } catch { return ''; } };
const saveKey = (token, v) => { try { if (v) sessionStorage.setItem(store(token), v); else sessionStorage.removeItem(store(token)); } catch { /* ignore */ } };

async function getJson(url, headers) {
  const res = await fetch(url, { headers });
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const err = new Error(typeof body?.detail === 'string' ? body.detail : `Error ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

function Shell({ children, sharedBy }) {
  return (
    <div className="min-h-[100dvh] bg-zinc-950 text-zinc-100">
      <div className="pointer-events-none fixed inset-x-0 top-0 h-72 bg-gradient-to-b from-accent/10 to-transparent" />
      <header className="relative mx-auto flex max-w-5xl items-center justify-between gap-4 px-5 py-5">
        <div className="flex items-center gap-2.5">
          <div className="grid h-8 w-8 place-items-center rounded-lg bg-accent text-accent-foreground"><FolderArchive className="h-[18px] w-[18px]" /></div>
          <span className="text-sm font-semibold tracking-tight text-zinc-200">Zerko File Manager</span>
        </div>
        {sharedBy && <span className="truncate text-xs text-zinc-500">Shared by <span className="text-zinc-300">{sharedBy}</span></span>}
      </header>
      <main className="relative mx-auto max-w-5xl px-5 pb-16">{children}</main>
      <footer className="relative pb-8 text-center text-xs text-zinc-700">Sent with Zerko File Manager</footer>
    </div>
  );
}

function Card({ children, className }) {
  return <div className={cn('mx-auto w-full max-w-md rounded-3xl border border-zinc-800 bg-zinc-900/70 p-8 text-center shadow-2xl shadow-black/40 backdrop-blur', className)}>{children}</div>;
}

// ------------------------------------------------------------------ preview
function PublicPreview({ entry, url, onClose, onNext, onPrev }) {
  useEffect(() => {
    const k = (e) => { if (e.key === 'Escape') onClose(); if (e.key === 'ArrowRight') onNext?.(); if (e.key === 'ArrowLeft') onPrev?.(); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose, onNext, onPrev]);
  const src = url(entry, true);
  let body;
  if (entry.kind === 'image' && isNativeImage(entry)) body = <img src={src} alt={entry.name} className="max-h-full max-w-full rounded-xl object-contain" />;
  else if (entry.kind === 'video' && canPlayVideo(entry)) body = <video src={src} controls autoPlay className="max-h-full max-w-full rounded-xl bg-black" />;
  else if (entry.kind === 'audio' && canPlayAudio(entry)) body = <audio src={src} controls autoPlay className="w-full max-w-lg" />;
  else if (entry.kind === 'pdf') body = <iframe title={entry.name} src={src} className="h-full w-full rounded-xl bg-white" />;
  else body = (
    <div className="text-center">
      <KindTile kind={entry.kind} ext={entry.ext} className="mx-auto h-40 w-40 rounded-3xl" />
      <p className="mt-5 text-sm text-zinc-400">No preview for this kind of file.</p>
    </div>
  );
  return createPortal(
    <div className="fixed inset-0 z-50 flex flex-col bg-zinc-950/95 backdrop-blur-sm" role="dialog" aria-modal="true">
      <div className="flex items-center gap-3 border-b border-zinc-800 px-4 py-3">
        <button onClick={onClose} aria-label="Close" className="rounded-lg p-2 text-zinc-400 hover:bg-zinc-800 hover:text-white"><X className="h-5 w-5" /></button>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{entry.name}</p>
          <p className="text-xs text-zinc-500">{formatBytes(entry.size)}</p>
        </div>
        <button onClick={() => startDownload(url(entry, false), entry.name)}
                className="inline-flex items-center gap-2 rounded-lg bg-zinc-100 px-3.5 py-2 text-sm font-semibold text-zinc-950 hover:bg-white">
          <Download className="h-4 w-4" /> Download
        </button>
      </div>
      <div className="grid min-h-0 flex-1 place-items-center p-4 sm:p-8">{body}</div>
    </div>,
    document.body,
  );
}

// ------------------------------------------------------------------ page
export default function FileSharePage() {
  const { token } = useParams();
  const [state, setState] = useState({ status: 'loading' });     // loading | locked | error | ready
  const [info, setInfo] = useState(null);
  const [access, setAccess] = useState(() => readKey(token));
  const [password, setPassword] = useState('');
  const [wrong, setWrong] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sub, setSub] = useState('');
  const [list, setList] = useState(null);
  const [listError, setListError] = useState('');
  const [preview, setPreview] = useState(null);

  const q = useCallback((extra = {}) => {
    const p = new URLSearchParams();
    Object.entries(extra).forEach(([k, v]) => { if (v) p.set(k, v); });
    if (access) p.set('access', access);
    return p.toString();
  }, [access]);

  const url = useCallback((entry, inline) => `${base(token)}/download?${q({ sub: entry.path || sub, inline: inline ? 'true' : '' })}`, [token, q, sub]);
  const thumb = useCallback((entry) => `${base(token)}/thumb?${q({ sub: entry.path || '', w: 360 })}`, [token, q]);

  const load = useCallback(async (pw) => {
    setBusy(true);
    try {
      const headers = pw ? { 'X-Share-Password': pw } : undefined;
      const qs = access && !pw ? `?access=${encodeURIComponent(access)}` : '';
      const r = await getJson(`${base(token)}/info${qs}`, headers);
      if (r.locked) {
        if (pw) setWrong(true);
        if (access) { setAccess(''); saveKey(token, ''); }
        setState({ status: 'locked' });
      } else {
        if (r.access) { setAccess(r.access); saveKey(token, r.access); }
        setInfo(r); setWrong(false); setState({ status: 'ready' });
      }
    } catch (e) {
      setState({ status: 'error', message: e.message, code: e.status });
    } finally { setBusy(false); }
  }, [token, access]);

  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [token]);

  // folder contents
  useEffect(() => {
    if (state.status !== 'ready' || !info?.is_dir) return undefined;
    let dead = false;
    setList(null); setListError('');
    getJson(`${base(token)}/list?${q({ sub })}`)
      .then((r) => { if (!dead) setList(r); })
      .catch((e) => { if (!dead) setListError(e.message); });
    return () => { dead = true; };
  }, [state.status, info, sub, token, q]);

  const entries = useMemo(() => (list ? sortEntries(list.entries, 'name', 'asc') : []), [list]);
  const files = entries.filter((e) => !e.is_dir);

  // ---------------------------------------------------------- states
  if (state.status === 'loading') {
    return <Shell><div className="grid place-items-center py-40"><Loader2 className="h-6 w-6 animate-spin text-zinc-500" /></div></Shell>;
  }

  if (state.status === 'error') {
    const gone = state.code === 410;
    return (
      <Shell>
        <Card className="mt-16">
          <div className="mx-auto grid h-16 w-16 place-items-center rounded-2xl bg-zinc-800">
            {gone ? <Clock className="h-7 w-7 text-zinc-400" /> : <AlertCircle className="h-7 w-7 text-zinc-400" />}
          </div>
          <h1 className="mt-5 text-xl font-semibold">{gone ? 'This link has expired' : 'This link is not available'}</h1>
          <p className="mt-2 text-sm leading-relaxed text-zinc-400">
            {state.code === 429 ? state.message : gone ? 'Ask the person who sent it to share it again.' : 'It may have been turned off, or the file may have been moved. Ask the person who sent it for a new link.'}
          </p>
        </Card>
      </Shell>
    );
  }

  if (state.status === 'locked') {
    return (
      <Shell>
        <Card className="mt-16">
          <div className="mx-auto grid h-16 w-16 place-items-center rounded-2xl bg-accent/15"><Lock className="h-7 w-7 text-accent" /></div>
          <h1 className="mt-5 text-xl font-semibold">This link is protected</h1>
          <p className="mt-2 text-sm text-zinc-400">Enter the password you were given to continue.</p>
          <form className="mt-6 space-y-3" onSubmit={(e) => { e.preventDefault(); if (password) load(password); }}>
            <input type="password" autoFocus value={password} onChange={(e) => { setPassword(e.target.value); setWrong(false); }}
                   placeholder="Password" autoComplete="off"
                   className={cn('w-full rounded-xl border bg-zinc-950 px-4 py-3 text-center text-sm outline-none transition focus:ring-2',
                     wrong ? 'border-red-500/60 focus:ring-red-500/30' : 'border-zinc-700 focus:border-accent focus:ring-accent/30')} />
            {wrong && <p className="text-xs text-red-400">That password is not right.</p>}
            <button type="submit" disabled={busy || !password}
                    className="flex w-full items-center justify-center gap-2 rounded-xl bg-accent py-3 text-sm font-semibold text-accent-foreground transition hover:bg-accent-hi disabled:opacity-50">
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />} Unlock
            </button>
          </form>
        </Card>
      </Shell>
    );
  }

  // ---------------------------------------------------------- a single file
  if (!info.is_dir) {
    const entry = { ...info, path: '', ext: (info.name.split('.').pop() || '').toLowerCase() };
    const canView = ['image', 'video', 'audio', 'pdf'].includes(info.kind);
    return (
      <Shell sharedBy={info.shared_by}>
        <div className="mx-auto mt-6 max-w-xl overflow-hidden rounded-3xl border border-zinc-800 bg-zinc-900/70 shadow-2xl shadow-black/40 backdrop-blur">
          <button type="button" disabled={!canView} onClick={() => setPreview(entry)}
                  className={cn('group relative block w-full', canView ? 'cursor-zoom-in' : 'cursor-default')}>
            <Thumb entry={{ ...entry, thumb: info.thumb }} src={info.thumb ? thumb(entry) : null} className="aspect-[16/10] w-full" />
            {canView && (
              <span className="absolute inset-0 grid place-items-center bg-black/0 opacity-0 transition group-hover:bg-black/40 group-hover:opacity-100">
                <span className="inline-flex items-center gap-2 rounded-full bg-zinc-900/90 px-4 py-2 text-sm"><Eye className="h-4 w-4" /> Preview</span>
              </span>
            )}
          </button>
          <div className="p-6">
            <h1 className="break-words text-xl font-semibold leading-snug">{info.name}</h1>
            <p className="mt-1.5 text-sm text-zinc-500">{kindLabel(entry)} - {formatBytes(info.size)} - {formatWhen(info.mtime)}</p>
            <button onClick={() => startDownload(url(entry, false), info.name)}
                    className="mt-6 flex w-full items-center justify-center gap-2 rounded-xl bg-accent py-3.5 text-[15px] font-semibold text-accent-foreground shadow-lg shadow-black/30 transition hover:bg-accent-hi">
              <Download className="h-5 w-5" /> Download
            </button>
            {info.expires_at && <p className="mt-4 text-center text-xs text-zinc-600">This link works until {formatWhen(info.expires_at, { long: true })}</p>}
          </div>
        </div>
        {preview && <PublicPreview entry={preview} url={url} onClose={() => setPreview(null)} />}
      </Shell>
    );
  }

  // ---------------------------------------------------------- a folder
  const crumbs = [{ name: info.name, path: '' }, ...(list?.breadcrumbs || [])];
  const idx = preview ? files.findIndex((f) => f.path === preview.path) : -1;
  const zipHref = `${base(token)}/zip?${q({ sub })}`;
  return (
    <Shell sharedBy={info.shared_by}>
      <div className="mt-4 flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <nav className="flex flex-wrap items-center text-sm">
            {crumbs.map((c, i) => (
              <span key={c.path || 'root'} className="flex items-center">
                {i > 0 && <ChevronRight className="h-4 w-4 text-zinc-600" />}
                <button onClick={() => setSub(c.path)} className={cn('rounded-lg px-2 py-1 transition', i === crumbs.length - 1 ? 'font-semibold text-zinc-50' : 'text-zinc-400 hover:bg-zinc-800')}>{c.name}</button>
              </span>
            ))}
          </nav>
          <p className="mt-1 px-2 text-xs text-zinc-500">
            {list ? [entries.some((e) => e.is_dir) && `${entries.filter((e) => e.is_dir).length} folders`, `${files.length} file${files.length === 1 ? '' : 's'}`, formatBytes(files.reduce((s, f) => s + f.size, 0))].filter(Boolean).join(' - ') : ' '}
            {info.expires_at ? ` - link works until ${formatWhen(info.expires_at)}` : ''}
          </p>
        </div>
        <button onClick={() => startDownload(zipHref)}
                className="inline-flex items-center gap-2 rounded-xl bg-accent px-5 py-3 text-sm font-semibold text-accent-foreground shadow-lg shadow-black/30 transition hover:bg-accent-hi">
          <Download className="h-[18px] w-[18px]" /> Download {sub ? 'this folder' : 'everything'} (.zip)
        </button>
      </div>

      <div className="mt-6">
        {listError && <p className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">{listError}</p>}
        {!list && !listError && (
          <div className="grid gap-3.5 [grid-template-columns:repeat(auto-fill,minmax(11rem,1fr))]">
            {Array.from({ length: 8 }).map((_, i) => <div key={i} className="aspect-[4/3.6] animate-pulse rounded-2xl bg-zinc-900" />)}
          </div>
        )}
        {list && entries.length === 0 && <p className="py-20 text-center text-sm text-zinc-500">This folder is empty.</p>}
        {list && entries.length > 0 && (
          <div className="grid gap-3.5 [grid-template-columns:repeat(auto-fill,minmax(11rem,1fr))]">
            {entries.map((e) => e.is_dir ? (
              <button key={e.path} onClick={() => setSub(e.path)}
                      className="group flex items-center gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/60 px-4 py-4 text-left transition hover:border-zinc-600 hover:bg-zinc-900 [grid-column:span_1]">
                <KindGlyph kind="folder" className="h-6 w-6 shrink-0" />
                <span className="truncate text-sm font-medium" title={e.name}>{e.name}</span>
              </button>
            ) : (
              <div key={e.path} className="group overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-900/60 transition hover:border-zinc-600">
                <button type="button" onClick={() => setPreview(e)} className="block w-full p-2 pb-0">
                  <Thumb entry={e} src={e.thumb ? thumb(e) : null} className="aspect-[4/3] w-full rounded-xl" />
                </button>
                <div className="flex items-center gap-2 px-3 py-2.5">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13px] font-medium" title={e.name}>{e.name}</p>
                    <p className="text-[11px] text-zinc-500">{formatBytes(e.size)}</p>
                  </div>
                  <button onClick={() => startDownload(url(e, false), e.name)} aria-label={`Download ${e.name}`} title="Download"
                          className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100">
                    <Download className="h-4 w-4" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      {preview && idx >= 0 && (
        <PublicPreview entry={preview} url={url} onClose={() => setPreview(null)}
                       onNext={idx < files.length - 1 ? () => setPreview(files[idx + 1]) : undefined}
                       onPrev={idx > 0 ? () => setPreview(files[idx - 1]) : undefined} />
      )}
    </Shell>
  );
}
