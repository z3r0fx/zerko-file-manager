import { useState, useEffect, useCallback, useContext } from 'react';
import { AuthContext } from '../../context/AuthContext';
import { apiCall } from '../../lib/api';
import { Zap, ShieldAlert, DatabaseBackup, Check, Play, Square, FolderSearch, Loader2 } from 'lucide-react';

/**
 * The jobs that keep the library healthy: proxy backlog, catalog backups and
 * the security checks that matter now that this is reachable from outside.
 * Admin only - the endpoints behind it are too.
 */
export default function MaintenancePanel() {
  const { user } = useContext(AuthContext);
  const [proxy, setProxy] = useState(null);
  const [scan, setScan] = useState(null);
  const [backups, setBackups] = useState(null);
  const [security, setSecurity] = useState(null);
  const [busy, setBusy] = useState(null);
  const [note, setNote] = useState(null);
  const [thumbs, setThumbs] = useState(null);   // thumbnail repair progress + reasons

  const isAdmin = user?.role === 'admin';

  const load = useCallback(async () => {
    if (!isAdmin) return;
    const safe = (p) => apiCall(p).catch(() => null);
    const [ps, bk, sec, sc] = await Promise.all([
      safe('/api/proxies/status'), safe('/api/admin/backups'),
      safe('/api/security-check'), safe('/api/rescan/status'),
    ]);
    setProxy(ps); setBackups(bk); setSecurity(sec); setScan(sc);
  }, [isAdmin]);

  useEffect(() => { load(); }, [load]);

  // Keep the panel live while a scan is running, then stop.
  useEffect(() => {
    if (!scan?.running) return undefined;
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [scan?.running, load]);

  // The thumbnail repair runs in the background; follow it until it finishes.
  useEffect(() => {
    if (!thumbs?.running) return undefined;
    const t = setInterval(async () => {
      const r = await apiCall('/api/thumbnails/repair/status').catch(() => null);
      if (r) setThumbs(r);
    }, 1200);
    return () => clearInterval(t);
  }, [thumbs?.running]);

  if (!isAdmin) return null;

  const say = (m) => { setNote(m); setTimeout(() => setNote(null), 5000); };

  const run = async (key, path, body) => {
    setBusy(key);
    try {
      const r = await apiCall(path, { method: 'POST', body: JSON.stringify(body || {}) });
      if (key === 'thumbs' && (r?.status === 'started' || r?.status === 'already_running')) {
        setThumbs({ running: true, message: 'Starting…', failures: [] });
      } else if (r?.status === 'started' && key === 'scan') say('Scanning your media folder…');
      else if (r?.queued != null) say(`Queued ${r.queued} clips for proxy generation.`);
      else if (r?.cancelled != null) say(`Cancelled ${r.cancelled} queued jobs.`);
      else if (r?.name) say(`Backup written: ${r.name}`);
      await load();
    } catch (e) {
      say(e.message || 'That did not work');
    }
    setBusy(null);
  };

  const warnings = security?.warnings || [];

  return (
    <div className="space-y-4">
      {warnings.length > 0 && (
        <div className="rounded-lg border border-amber-600/40 bg-amber-600/10 p-4">
          <h2 className="mb-2 flex items-center gap-2 font-semibold text-amber-300">
            <ShieldAlert className="h-5 w-5" /> Security
          </h2>
          <ul className="space-y-2">
            {warnings.map((w, i) => (
              <li key={i} className="text-sm">
                <span className={w.level === 'critical' ? 'text-red-300' : 'text-amber-200'}>
                  {w.level === 'critical' ? '● ' : '○ '}{w.message}
                </span>
                {w.action && <span className="ml-1 text-zinc-400">{w.action}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Scan for new files */}
        <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
          <h2 className="mb-3 flex items-center gap-2 font-semibold text-zinc-200">
            <FolderSearch className="h-5 w-5 text-accent" /> Media folder
          </h2>
          {scan?.running ? (
            <div className="flex items-start gap-3">
              <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-accent" />
              <div className="min-w-0">
                <p className="text-sm text-zinc-200">Scanning…</p>
                <p className="truncate font-mono text-xs text-zinc-500">{scan.message}</p>
              </div>
            </div>
          ) : (
            <>
              <p className="mb-3 text-xs leading-relaxed text-zinc-500">
                Reads your media folder and adds anything new. Run it after you
                copy footage in. Your files are only read, never moved.
              </p>
              {scan?.stats && (
                <p className="mb-2 font-mono text-[11px] text-emerald-500">
                  last scan: {scan.stats.added ?? 0} added · {scan.stats.folders ?? 0} folders
                </p>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <button
                  onClick={() => run('scan', '/api/rescan', { queue_proxies: true })}
                  disabled={busy === 'scan'}
                  className="flex items-center gap-1.5 rounded bg-accent px-3 py-1.5 text-xs font-medium text-accent-foreground transition hover:bg-accent-hi disabled:opacity-40"
                >
                  <FolderSearch className="h-3.5 w-3.5" /> Scan for new files
                </button>
                <button
                  onClick={() => run('thumbs', '/api/thumbnails/repair', {})}
                  disabled={busy === 'thumbs'}
                  title="Redraw thumbnails that failed the first time"
                  className="flex items-center gap-1.5 rounded border border-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-200 transition hover:border-zinc-500 hover:bg-zinc-800 disabled:opacity-40"
                >
                  <FolderSearch className="h-3.5 w-3.5" /> Fix missing thumbnails
                </button>
              </div>
              {thumbs && (
                <div className="mt-3 rounded border border-zinc-800 bg-zinc-950 p-3 text-xs">
                  <p className={thumbs.running ? 'text-zinc-400' : 'text-zinc-200'}>
                    {thumbs.running ? 'Redrawing thumbnails… ' : ''}{thumbs.message}
                    {thumbs.running && thumbs.total ? ` (${thumbs.fixed || 0} fixed)` : ''}
                  </p>
                  {!thumbs.running && thumbs.failures?.length > 0 && (
                    <ul className="mt-2 max-h-40 space-y-1.5 overflow-y-auto pr-1">
                      {thumbs.failures.map((f) => (
                        <li key={f.id} className="leading-snug text-zinc-500">
                          <span className="font-mono text-zinc-300">{f.filename}</span> — {f.reason}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        {/* Proxies */}
        <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
          <h2 className="mb-3 flex items-center gap-2 font-semibold text-zinc-200">
            <Zap className="h-5 w-5 text-blue-400" /> Proxies
          </h2>
          {proxy ? (
            <>
              <div className="mb-2 h-2 overflow-hidden rounded-full bg-zinc-800">
                <div className="h-full rounded-full bg-blue-500 transition-all"
                     style={{ width: `${proxy.percent}%` }} />
              </div>
              <p className="mb-3 font-mono text-xs text-zinc-400">
                {proxy.completed} of {proxy.total} done ({proxy.percent}%)
                {proxy.queued > 0 && <span className="text-blue-400"> · {proxy.queued} queued</span>}
                {proxy.failed > 0 && <span className="text-red-400"> · {proxy.failed} failed</span>}
              </p>
              <p className="mb-3 text-xs text-zinc-500">
                Proxies are the small streaming copies. Without them, anyone watching
                remotely pulls the full-size original down your upload link.
              </p>
              <div className="flex gap-2">
                <button
                  onClick={() => run('proxy', '/api/proxies/generate-missing')}
                  disabled={busy === 'proxy' || proxy.missing === 0}
                  className="flex items-center gap-1.5 rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-blue-500 disabled:opacity-40"
                >
                  <Play className="h-3.5 w-3.5" />
                  {proxy.missing === 0 ? 'All done' : `Generate ${proxy.missing} missing`}
                </button>
                {proxy.queued > 0 && (
                  <button
                    onClick={() => run('cancel', '/api/proxies/cancel')}
                    disabled={busy === 'cancel'}
                    className="flex items-center gap-1.5 rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
                  >
                    <Square className="h-3.5 w-3.5" /> Stop
                  </button>
                )}
              </div>
            </>
          ) : <p className="text-sm text-zinc-500">…</p>}
        </div>

        {/* Backups */}
        <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
          <h2 className="mb-3 flex items-center gap-2 font-semibold text-zinc-200">
            <DatabaseBackup className="h-5 w-5 text-emerald-400" /> Catalog backups
          </h2>
          {backups ? (
            <>
              <p className="mb-2 text-xs text-zinc-500">
                Transcripts, tags, ratings and folder structure. Runs daily, keeps {backups.keep}.
              </p>
              {backups.backups?.length ? (
                <p className="mb-3 flex items-center gap-1.5 font-mono text-xs text-emerald-400">
                  <Check className="h-3.5 w-3.5" />
                  latest {new Date(backups.backups[0].created_at).toLocaleString()} ·
                  {' '}{backups.backups[0].size_formatted} · {backups.backups.length} kept
                </p>
              ) : (
                <p className="mb-3 text-xs text-amber-400">No backups yet.</p>
              )}
              <button
                onClick={() => run('backup', '/api/admin/backups')}
                disabled={busy === 'backup'}
                className="rounded bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-emerald-500 disabled:opacity-40"
              >
                {busy === 'backup' ? 'Backing up…' : 'Back up now'}
              </button>
            </>
          ) : <p className="text-sm text-zinc-500">…</p>}
        </div>
      </div>

      {note && <p className="text-sm text-zinc-400">{note}</p>}
    </div>
  );
}
