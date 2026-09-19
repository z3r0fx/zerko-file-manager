import { useState, useEffect, useCallback, useContext } from 'react';
import { AuthContext } from '../../context/AuthContext';
import { apiCall } from '../../lib/api';
import {
  Download, RefreshCw, CheckCircle2, Loader2, Github, Settings2, AlertCircle,
} from 'lucide-react';

/**
 * Updates from GitHub.
 *
 * The flow is deliberately three steps - check, download, restart - rather
 * than one. Downloading is safe at any time; replacing the files is only safe
 * between runs, so applying always means a restart. That is what stops an
 * update landing halfway through an upload or a transcode.
 */
export default function UpdatePanel() {
  const { user } = useContext(AuthContext);
  const isAdmin = user?.role === 'admin';

  const [st, setSt] = useState(null);
  const [busy, setBusy] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const [owner, setOwner] = useState('');
  const [repo, setRepo] = useState('');
  const [applying, setApplying] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = await apiCall('/api/updates/status');
      setSt(s);
      setOwner(s.settings?.owner || '');
      setRepo(s.settings?.repo || '');
    } catch { /* not admin, or offline */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Poll while something is in flight.
  useEffect(() => {
    if (!st?.downloading && !st?.checking) return undefined;
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [st?.downloading, st?.checking, load]);

  // After applying, wait for the server to come back, then reload.
  useEffect(() => {
    if (!applying) return undefined;
    const t = setInterval(async () => {
      try {
        const r = await fetch('/api/setup/status', { cache: 'no-store' });
        if (r.ok) window.location.reload();
      } catch { /* still restarting */ }
    }, 3000);
    return () => clearInterval(t);
  }, [applying]);

  if (!isAdmin || !st) return null;

  const info = st.last_check;
  const available = info?.update_available;

  const act = async (key, path, body) => {
    setBusy(key);
    try { await apiCall(path, { method: 'POST', body: JSON.stringify(body || {}) }); }
    catch { /* surfaced in status */ }
    await load();
    setBusy(null);
  };

  const applyNow = async () => {
    if (!window.confirm(
      `Update to version ${st.staged_version} and restart?\n\n`
      + `Zerko will be unavailable for about a minute. Anything uploading or `
      + `transcoding right now will be interrupted.`)) return;
    setApplying(true);
    try { await apiCall('/api/updates/apply', { method: 'POST', body: '{}' }); }
    catch { /* the server exits mid-response, which is expected */ }
  };

  if (applying) {
    return (
      <div className="rounded-lg border border-[#ff5c1f]/40 bg-[#ff5c1f]/10 p-4">
        <p className="flex items-center gap-3 text-sm text-zinc-100">
          <Loader2 className="h-5 w-5 animate-spin text-[#ff5c1f]" />
          Updating and restarting — this page will come back on its own.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <div className="mb-3 flex items-center gap-2">
        <Github className="h-5 w-5 text-zinc-400" />
        <h2 className="font-semibold text-zinc-200">Updates</h2>
        <span className="rounded bg-zinc-800 px-2 py-0.5 font-mono text-[11px] text-zinc-400">
          v{st.current_version}
        </span>
        <button onClick={() => setShowSettings((v) => !v)}
                className="ml-auto text-zinc-500 hover:text-zinc-200" title="Update settings">
          <Settings2 className="h-4 w-4" />
        </button>
      </div>

      {!st.settings?.owner ? (
        <p className="text-xs text-zinc-500">
          No update source set yet. Open the settings icon above and enter the
          GitHub account and repository to pull releases from.
        </p>
      ) : st.staged_version ? (
        <>
          <p className="mb-3 flex items-center gap-2 text-sm text-emerald-400">
            <CheckCircle2 className="h-4 w-4" />
            Version {st.staged_version} is downloaded and ready.
          </p>
          <button onClick={applyNow}
                  className="rounded bg-[#ff5c1f] px-4 py-2 text-sm font-medium text-black transition hover:bg-[#ff7a45]">
            Install and restart
          </button>
          <p className="mt-2 text-[11px] text-zinc-600">
            Your library, settings and login are untouched. If the new version
            fails to start, the previous one is put back automatically.
          </p>
        </>
      ) : available ? (
        <>
          <p className="mb-2 text-sm text-zinc-200">
            Version <strong className="font-mono text-[#ff5c1f]">{info.latest}</strong> is available
            <span className="text-zinc-500"> — you have {st.current_version}</span>
          </p>
          {info.notes && (
            <pre className="mb-3 max-h-32 overflow-y-auto whitespace-pre-wrap rounded bg-zinc-950 p-2 text-[11px] leading-relaxed text-zinc-400">
              {info.notes}
            </pre>
          )}
          <button onClick={() => act('dl', '/api/updates/download')}
                  disabled={busy === 'dl' || st.downloading}
                  className="flex items-center gap-2 rounded bg-[#ff5c1f] px-4 py-2 text-sm font-medium text-black transition hover:bg-[#ff7a45] disabled:opacity-50">
            {st.downloading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
            {st.downloading ? 'Downloading…' : 'Download update'}
          </button>
        </>
      ) : (
        <div className="flex items-center gap-3">
          <p className="text-sm text-zinc-400">
            {info?.ok === false
              ? <span className="flex items-center gap-1.5 text-amber-400">
                  <AlertCircle className="h-3.5 w-3.5" /> {info.error}
                </span>
              : 'You are on the latest version.'}
          </p>
          <button onClick={() => act('check', '/api/updates/check')}
                  disabled={busy === 'check' || st.checking}
                  className="ml-auto flex items-center gap-1.5 rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50">
            <RefreshCw className={'h-3.5 w-3.5 ' + (st.checking ? 'animate-spin' : '')} />
            Check now
          </button>
        </div>
      )}

      {st.message && <p className="mt-2 font-mono text-[11px] text-zinc-500">{st.message}</p>}

      {showSettings && (
        <div className="mt-4 space-y-3 border-t border-zinc-800 pt-3">
          <div className="flex gap-2">
            <label className="flex-1">
              <span className="mb-1 block text-[11px] text-zinc-500">GitHub account</span>
              <input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="your-username"
                     className="w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-[#ff5c1f]" />
            </label>
            <label className="flex-1">
              <span className="mb-1 block text-[11px] text-zinc-500">Repository</span>
              <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="zerko-file-manager"
                     className="w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-[#ff5c1f]" />
            </label>
          </div>

          <label className="flex cursor-pointer items-start gap-2">
            <input type="checkbox" checked={!!st.settings?.auto_check}
                   onChange={(e) => act('set', '/api/updates/settings', { auto_check: e.target.checked })}
                   className="mt-0.5 h-3.5 w-3.5 accent-[#ff5c1f]" />
            <span>
              <span className="block text-xs text-zinc-300">Check for updates automatically</span>
              <span className="block text-[11px] text-zinc-600">Looks once an hour. Never installs on its own.</span>
            </span>
          </label>

          <label className="flex cursor-pointer items-start gap-2">
            <input type="checkbox" checked={!!st.settings?.auto_apply}
                   onChange={(e) => act('set', '/api/updates/settings', { auto_apply: e.target.checked })}
                   className="mt-0.5 h-3.5 w-3.5 accent-[#ff5c1f]" />
            <span>
              <span className="block text-xs text-zinc-300">Download updates in the background</span>
              <span className="block text-[11px] text-zinc-600">
                Still only installs when you restart, so nothing is interrupted mid-job.
              </span>
            </span>
          </label>

          <button onClick={() => act('set', '/api/updates/settings', { owner, repo })}
                  className="rounded bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 hover:bg-white">
            Save source
          </button>
        </div>
      )}
    </div>
  );
}
