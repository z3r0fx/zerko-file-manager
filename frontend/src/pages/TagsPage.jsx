import { useState, useEffect, useCallback, useContext } from 'react';
import { useNavigate } from 'react-router-dom';
import { AuthContext } from '../context/AuthContext';
import { apiCall } from '../lib/api';
import {
  Tags as TagsIcon, Sparkles, Loader2, Mic, Camera, FolderTree,
  Layers, Search, Eraser, RefreshCw, AlertCircle,
} from 'lucide-react';

const SOURCE_META = {
  spoken: { label: 'From speech',  icon: Mic,        cls: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30',
            hint: 'Pulled out of what is actually said on camera' },
  camera: { label: 'From camera',  icon: Camera,     cls: 'text-sky-400 bg-sky-500/10 border-sky-500/30',
            hint: 'Read from the file itself — which camera shot it' },
  folder: { label: 'From folders', icon: FolderTree, cls: 'text-amber-400 bg-amber-500/10 border-amber-500/30',
            hint: 'The shoot or project a clip is filed under' },
  stage:  { label: 'Stage',        icon: Layers,     cls: 'text-purple-400 bg-purple-500/10 border-purple-500/30',
            hint: 'Where a clip is in the edit — raw, graded, rendered' },
};

/**
 * Auto-tagging: what exists, where it came from, and how to regenerate it.
 *
 * Tagging used to be a script you had to know about and run from a terminal,
 * which meant a freshly indexed library sat there with an empty Tags list and
 * no hint that anything was missing.
 */
export default function TagsPage() {
  const { user } = useContext(AuthContext);
  const navigate = useNavigate();
  const isAdmin = user?.role === 'admin';

  const [data, setData] = useState(null);
  const [rules, setRules] = useState(null);
  const [query, setQuery] = useState('');
  const [source, setSource] = useState('all');
  const [busy, setBusy] = useState(false);
  const [showRules, setShowRules] = useState(false);
  const [useTranscripts, setUseTranscripts] = useState(true);

  const load = useCallback(async () => {
    try { setData(await apiCall('/api/tags/overview')); } catch { /* keep the last view */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Poll only while work is actually happening.
  useEffect(() => {
    if (!data?.running) return undefined;
    const t = setInterval(load, 1200);
    return () => clearInterval(t);
  }, [data?.running, load]);

  const run = async () => {
    setBusy(true);
    try {
      await apiCall('/api/tags/auto-generate', {
        method: 'POST',
        body: JSON.stringify({ include_transcripts: useTranscripts }),
      });
      await load();
    } catch { /* already running, or not allowed */ }
    setBusy(false);
  };

  const openRules = async () => {
    setShowRules((v) => !v);
    if (!rules) {
      try { setRules(await apiCall('/api/tags/rules')); } catch { setRules({}); }
    }
  };

  const clearTag = async (t) => {
    if (!window.confirm(
      `Remove the "${t.name}" tag from all ${t.count} files?\n\n`
      + `The tag itself stays. Running auto-tagging again will re-apply it `
      + `unless you change the rule.`)) return;
    await apiCall(`/api/tags/${t.id}/assignments`, { method: 'DELETE' });
    load();
  };

  if (!data) return <div className="p-6 text-zinc-500">Loading…</div>;

  const shown = data.tags.filter((t) =>
    (source === 'all' || t.source === source)
    && (!query || t.name.toLowerCase().includes(query.toLowerCase())));

  const coverage = data.files_total
    ? Math.round((data.files_tagged / data.files_total) * 100) : 0;

  return (
    <div className="h-full overflow-y-auto px-6 py-6">
      <div className="mx-auto max-w-5xl">
        <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-medium text-zinc-100">
              <TagsIcon className="h-5 w-5 text-accent" /> Auto-tagging
            </h1>
            <p className="mt-1 text-sm text-zinc-500">
              Tags are worked out from your folders, your cameras, and what you say on camera.
            </p>
          </div>
          <button onClick={load}
                  className="flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800">
            <RefreshCw className="h-4 w-4" /> Refresh
          </button>
        </div>

        {/* coverage */}
        <div className="mb-5 rounded-xl border border-zinc-800 bg-zinc-900/50 p-4">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <span className="text-sm text-zinc-300">
              <strong className="font-mono text-accent">{data.files_tagged.toLocaleString()}</strong>
              {' '}of {data.files_total.toLocaleString()} files have tags
            </span>
            <span className="font-mono text-xs text-zinc-500">
              {data.total_tags} tags · {data.total_assignments.toLocaleString()} assignments
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-zinc-800">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${coverage}%` }} />
          </div>
          {data.files_untagged > 0 && (
            <p className="mt-2 text-xs text-zinc-500">
              {data.files_untagged.toLocaleString()} files have no tags — usually B-roll with no speech
              and nothing distinctive in the folder name.
            </p>
          )}
        </div>

        {/* run it */}
        {isAdmin && (
          <div className="mb-5 rounded-xl border border-zinc-800 bg-zinc-900/50 p-4">
            {data.running ? (
              <div className="flex items-center gap-3">
                <Loader2 className="h-5 w-5 animate-spin text-accent" />
                <div>
                  <p className="text-sm text-zinc-200">Tagging…</p>
                  <p className="font-mono text-xs text-zinc-500">{data.message}</p>
                </div>
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-3">
                  <button onClick={run} disabled={busy}
                          className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition hover:bg-accent-hi disabled:opacity-50">
                    <Sparkles className="h-4 w-4" /> Run auto-tagging
                  </button>
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-zinc-400">
                    <input type="checkbox" checked={useTranscripts}
                           onChange={(e) => setUseTranscripts(e.target.checked)}
                           className="h-3.5 w-3.5 accent-accent" />
                    include what's said on camera
                    <span className="text-zinc-600">({data.files_with_transcript} transcribed)</span>
                  </label>
                  <button onClick={openRules} className="ml-auto text-xs text-zinc-500 hover:text-zinc-200">
                    {showRules ? 'Hide' : 'Show'} the rules
                  </button>
                </div>
                <p className="mt-2 text-xs text-zinc-600">
                  Safe to run whenever. It only adds tags that aren't already there — nothing is removed,
                  and your files are never touched.
                </p>
                {data.last_stats && (
                  <p className="mt-1.5 font-mono text-[11px] text-emerald-500">{data.message}</p>
                )}
              </>
            )}
          </div>
        )}

        {showRules && rules && (
          <div className="mb-5 rounded-xl border border-zinc-800 bg-zinc-950/60 p-4 text-xs">
            <p className="mb-3 text-zinc-500">
              A clip gets a tag when it matches one of these. Nothing is guessed by AI —
              these are fixed rules you can read.
            </p>
            {[['spoken', 'Said on camera'], ['camera', 'Camera detected in the file'], ['folder', 'Folder and filename']]
              .map(([k, label]) => (
                (rules[k] || []).length > 0 && (
                  <div key={k} className="mb-3">
                    <p className="mb-1 font-medium text-zinc-300">{label}</p>
                    <div className="flex flex-wrap gap-1.5">
                      {(rules[k] || []).map((r, i) => (
                        <span key={i} className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
                          {r.tag || r.make}
                        </span>
                      ))}
                    </div>
                  </div>
                )
              ))}
          </div>
        )}

        {/* filters */}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-600" />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a tag…"
                   className="rounded-lg border border-zinc-700 bg-zinc-950 py-1.5 pl-7 pr-3 text-sm text-zinc-200 outline-none focus:border-accent" />
          </div>
          {['all', 'spoken', 'camera', 'folder', 'stage'].map((k) => (
            <button key={k} onClick={() => setSource(k)}
                    className={'rounded-lg px-3 py-1.5 text-xs transition '
                      + (source === k ? 'bg-zinc-100 text-zinc-900' : 'border border-zinc-700 text-zinc-400 hover:text-zinc-100')}>
              {k === 'all' ? 'All' : SOURCE_META[k]?.label}
            </button>
          ))}
        </div>

        {/* the tags */}
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {shown.map((t) => {
            const meta = SOURCE_META[t.source] || SOURCE_META.folder;
            const Icon = meta.icon;
            return (
              <div key={t.id}
                   className="group flex items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900/40 p-3 transition hover:border-zinc-700">
                <span className={'flex h-7 w-7 shrink-0 items-center justify-center rounded border ' + meta.cls}
                      title={meta.hint}>
                  <Icon className="h-3.5 w-3.5" />
                </span>
                <button onClick={() => navigate('/browse')}
                        className="min-w-0 flex-1 text-left"
                        title="Filter the library by this tag from the sidebar">
                  <span className="block truncate text-sm text-zinc-200">{t.name}</span>
                  <span className="font-mono text-[10px] text-zinc-600">{meta.label}</span>
                </button>
                <span className="shrink-0 font-mono text-xs text-zinc-400">{t.count}</span>
                {isAdmin && t.count > 0 && (
                  <button onClick={() => clearTag(t)} title="Remove this tag from every file"
                          className="shrink-0 text-zinc-700 opacity-0 transition group-hover:opacity-100 hover:text-red-400">
                    <Eraser className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            );
          })}
        </div>

        {shown.length === 0 && (
          <div className="rounded-xl border border-dashed border-zinc-800 py-14 text-center">
            <AlertCircle className="mx-auto mb-3 h-7 w-7 text-zinc-700" />
            <p className="text-zinc-400">
              {data.tags.length === 0 ? 'No tags yet.' : 'Nothing matches that.'}
            </p>
            {data.tags.length === 0 && isAdmin && (
              <p className="mt-1 text-sm text-zinc-600">Press “Run auto-tagging” above.</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
