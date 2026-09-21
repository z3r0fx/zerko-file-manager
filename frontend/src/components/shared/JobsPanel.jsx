import { useEffect, useState, useContext } from 'react';
import { X, Film, Mic, Loader2, CheckCircle2, AlertCircle } from 'lucide-react';
import { DataContext } from '../../context/DataContext';
import { apiCall } from '../../lib/api';
import { cn } from '../../lib/utils';

const ACTIVE = ['queued', 'processing'];

export default function JobsPanel({ open, onClose }) {
  const dataContext = useContext(DataContext);
  const liveJobs = dataContext?.backgroundJobs || {};
  const [serverJobs, setServerJobs] = useState([]);

  // The SSE stream only carries jobs raised since this tab connected. Polling
  // the server as well means a job started before you opened the page - or by
  // someone else - still shows up here.
  useEffect(() => {
    if (!open) return undefined;
    let alive = true;
    const pull = async () => {
      try {
        const rows = await apiCall('/api/videos/jobs/status');
        if (alive && Array.isArray(rows)) setServerJobs(rows);
      } catch {
        /* panel keeps showing whatever the SSE stream gave us */
      }
    };
    pull();
    const id = setInterval(pull, 3000);
    return () => { alive = false; clearInterval(id); };
  }, [open]);

  if (!open) return null;

  const merged = {};
  serverJobs.forEach((j) => { if (j && j.job_id) merged[j.job_id] = j; });
  Object.values(liveJobs).forEach((j) => { if (j && j.job_id) merged[j.job_id] = { ...merged[j.job_id], ...j }; });

  const all = Object.values(merged);
  const active = all.filter((j) => ACTIVE.includes(j.status));
  const finished = all.filter((j) => !ACTIVE.includes(j.status)).slice(-8).reverse();

  const Row = ({ job }) => {
    const running = ACTIVE.includes(job.status);
    const failed = job.status === 'failed';
    const Icon = job.type === 'transcribe' ? Mic : Film;
    return (
      <div className="px-4 py-3 border-b border-zinc-800/70 last:border-b-0">
        <div className="flex items-center gap-2.5 mb-1.5">
          <Icon className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
          <span className="text-xs text-zinc-200 truncate flex-1" title={job.filename || job.job_id}>
            {job.filename || job.job_id}
          </span>
          {running && <Loader2 className="w-3.5 h-3.5 text-accent animate-spin shrink-0" />}
          {job.status === 'completed' && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 shrink-0" />}
          {failed && <AlertCircle className="w-3.5 h-3.5 text-red-500 shrink-0" />}
        </div>
        <div className="flex items-center gap-2 pl-6">
          <span className="text-[10px] uppercase tracking-wider text-zinc-500 w-20 shrink-0">
            {job.type === 'transcribe' ? 'Transcribe' : 'Proxy'}
          </span>
          <div className="h-1 flex-1 bg-zinc-800 rounded-full overflow-hidden">
            <div
              className={cn('h-full rounded-full transition-all duration-500',
                failed ? 'bg-red-500' : job.status === 'completed' ? 'bg-emerald-500' : 'bg-accent')}
              style={{ width: `${failed ? 100 : (job.progress || (job.status === 'completed' ? 100 : 4))}%` }}
            />
          </div>
          <span className="text-[10px] font-mono text-zinc-500 w-9 text-right shrink-0">
            {failed ? 'err' : `${job.progress || 0}%`}
          </span>
        </div>
        {job.note && (
          <p className="pl-6 mt-1 text-[10px] text-zinc-500">Skipped — {job.note}</p>
        )}
        {failed && job.error && (
          <p className="pl-6 mt-1.5 text-[10px] text-red-400/80 line-clamp-1" title={job.error}>
            {String(job.error).split('\n')[0].slice(0, 90)}
          </p>
        )}
      </div>
    );
  };

  return (
    <div
      style={{ left: 'max(1rem, calc(var(--sidebar-w) + 1rem))' }}
      className="fixed bottom-4 right-4 z-[9998] bg-zinc-900 border border-zinc-700 rounded-xl shadow-2xl animate-pop-in overflow-hidden sm:right-auto sm:w-[360px]"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-800 bg-zinc-900/95">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-zinc-100">Background jobs</h3>
          {active.length > 0 && (
            <span className="text-[10px] font-mono bg-accent text-zinc-950 px-1.5 py-0.5 rounded font-bold">
              {active.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {finished.length > 0 && (
            <button
              type="button"
              onClick={async () => {
                try { await apiCall('/api/videos/jobs/clear', { method: 'POST' }); } catch { /* noop */ }
                setServerJobs((rows) => rows.filter((j) => ACTIVE.includes(j.status)));
              }}
              className="text-[11px] text-zinc-500 hover:text-zinc-200 transition"
            >
              Clear finished
            </button>
          )}
          <button type="button" onClick={onClose} aria-label="Close jobs panel"
                  className="text-zinc-500 hover:text-zinc-200 transition">
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      <div className="max-h-[420px] overflow-y-auto">
        {active.length === 0 && finished.length === 0 ? (
          <div className="px-4 py-6 text-center">
            <p className="text-xs text-zinc-400 mb-2">Nothing running.</p>
            <p className="text-[11px] text-zinc-600 leading-relaxed">
              Jobs appear here when a video is uploaded (proxy generation) or
              when you transcribe clips. Creating a project doesn&rsquo;t queue any work.
            </p>
          </div>
        ) : (
          <>
            {active.map((j) => <Row key={j.job_id} job={j} />)}
            {finished.length > 0 && (
              <>
                <div className="px-4 py-1.5 bg-zinc-950/60 text-[10px] uppercase tracking-wider text-zinc-600">
                  Recent
                </div>
                {finished.map((j) => <Row key={j.job_id} job={j} />)}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
