import { useState, useEffect, useCallback } from 'react';
import { Link2, Copy, Check, Trash2, Heart, Eye, Clock, Download, RefreshCw, ExternalLink } from 'lucide-react';
import { apiCall } from '../lib/api';
import { useNavigate } from 'react-router-dom';

export default function SharesPage() {
  const navigate = useNavigate();
  const [shares, setShares] = useState([]);
  const [loading, setLoading] = useState(true);
  const [openId, setOpenId] = useState(null);
  const [selects, setSelects] = useState(null);
  const [copiedId, setCopiedId] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try { setShares(await apiCall('/api/shares')); } catch { setShares([]); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const openSelects = async (id) => {
    setOpenId(id); setSelects(null);
    try { setSelects(await apiCall(`/api/shares/${id}/selects`)); } catch { setSelects({ selects: [] }); }
  };

  const revoke = async (id) => {
    if (!window.confirm('Switch this link off? Anyone holding it loses access immediately.')) return;
    await apiCall(`/api/shares/${id}`, { method: 'DELETE' });
    load();
  };

  const copy = async (sh) => {
    const url = `${window.location.origin}${sh.url}`;
    try { await navigator.clipboard.writeText(url); } catch { /* clipboard blocked */ }
    setCopiedId(sh.id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const exportPicks = (fmt) => {
    window.location.href = `/api/shares/${openId}/export.${fmt}?token=${encodeURIComponent(localStorage.getItem('token') || '')}`;
  };

  return (
    <div className="h-full overflow-y-auto px-6 py-6">
      <div className="mx-auto max-w-5xl">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-medium text-zinc-100">Client links</h1>
            <p className="mt-1 text-sm text-zinc-500">
              Links you have sent out, and what came back.
            </p>
          </div>
          <button onClick={load} className="flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800">
            <RefreshCw className="h-4 w-4" /> Refresh
          </button>
        </div>

        {loading ? (
          <p className="text-zinc-500">Loading…</p>
        ) : shares.length === 0 ? (
          <div className="rounded-xl border border-dashed border-zinc-800 py-16 text-center">
            <Link2 className="mx-auto mb-3 h-8 w-8 text-zinc-700" />
            <p className="text-zinc-400">No client links yet.</p>
            <p className="mt-1 text-sm text-zinc-600">
              Right-click a folder in the sidebar, or select some clips, and choose Share.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {shares.map((sh) => {
              const dead = sh.revoked || sh.expired;
              return (
                <div key={sh.id}
                     className={'rounded-xl border p-4 transition ' +
                       (dead ? 'border-zinc-800 bg-zinc-900/30 opacity-60' : 'border-zinc-800 bg-zinc-900/60')}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <h2 className="truncate font-medium text-zinc-100">{sh.title || 'Untitled link'}</h2>
                      <p className="mt-1 flex flex-wrap items-center gap-3 font-mono text-[11px] text-zinc-500">
                        <span>{sh.count} clips</span>
                        <span className="flex items-center gap-1"><Eye className="h-3 w-3" />{sh.views}</span>
                        {sh.selects > 0 && (
                          <span className="flex items-center gap-1 text-[#ff5c1f]">
                            <Heart className="h-3 w-3" fill="currentColor" />{sh.selects} picked
                          </span>
                        )}
                        {sh.has_password && <span className="text-amber-500">password</span>}
                        {sh.allow_download && <span className="text-sky-400">downloads on</span>}
                        {sh.expires_at && (
                          <span className={'flex items-center gap-1 ' + (sh.expired ? 'text-red-400' : '')}>
                            <Clock className="h-3 w-3" />
                            {sh.expired ? 'expired' : `expires ${new Date(sh.expires_at).toLocaleDateString()}`}
                          </span>
                        )}
                        {sh.revoked && <span className="text-red-400">revoked</span>}
                      </p>
                    </div>

                    <div className="flex shrink-0 items-center gap-2">
                      {sh.selects > 0 && (
                        <button onClick={() => openSelects(sh.id)}
                                className="rounded-lg bg-[#ff5c1f]/15 px-3 py-1.5 text-xs text-[#ff5c1f] hover:bg-[#ff5c1f]/25">
                          View picks
                        </button>
                      )}
                      {!dead && (
                        <button onClick={() => copy(sh)}
                                className="flex items-center gap-1.5 rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800">
                          {copiedId === sh.id ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                          {copiedId === sh.id ? 'Copied' : 'Copy link'}
                        </button>
                      )}
                      {!sh.revoked && (
                        <button onClick={() => revoke(sh.id)} title="Revoke"
                                className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:border-red-500/50 hover:text-red-400">
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {openId != null && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4"
             onClick={() => setOpenId(null)}>
          <div className="flex max-h-[80vh] w-full max-w-2xl flex-col rounded-xl border border-zinc-700 bg-zinc-900"
               onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between border-b border-zinc-800 px-5 py-4">
              <h2 className="font-medium text-zinc-100">
                {selects?.share?.title || 'Picks'}
              </h2>
              <div className="flex gap-2">
                <button onClick={() => exportPicks('fcpxml')}
                        className="flex items-center gap-1.5 rounded-lg bg-[#ff5c1f] px-3 py-1.5 text-xs font-medium text-black hover:bg-[#ff7a45]">
                  <Download className="h-3.5 w-3.5" /> FCPXML for Resolve
                </button>
                <button onClick={() => exportPicks('csv')}
                        className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800">
                  CSV
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-4">
              {!selects ? (
                <p className="text-zinc-500">Loading…</p>
              ) : selects.selects.length === 0 ? (
                <p className="text-zinc-500">Nothing picked yet.</p>
              ) : (
                <ul className="space-y-2">
                  {selects.selects.map((s) => (
                    <li key={s.video_id}>
                      {/* The whole row is the button: going from "they liked
                          this" to having the clip open should be one click,
                          not a hunt through folders by filename. */}
                      <button
                        type="button"
                        onClick={() => {
                          setOpenId(null);
                          const q = new URLSearchParams();
                          if (s.folder_id != null) q.set('folder', String(s.folder_id));
                          q.set('video', String(s.video_id));
                          navigate(`/browse?${q.toString()}`);
                        }}
                        title="Open this clip in the library"
                        className={
                          'group flex w-full items-start gap-3 rounded-lg border p-3 text-left transition ' +
                          (s.picked
                            ? 'border-zinc-800 hover:border-[#ff5c1f]/60 hover:bg-zinc-800/40'
                            : 'border-zinc-800/60 bg-zinc-950/40 hover:border-zinc-700 hover:bg-zinc-800/30')
                        }
                      >
                        {s.thumbnail_path ? (
                          <img
                            src={s.thumbnail_path}
                            alt=""
                            loading="lazy"
                            className="h-10 w-16 shrink-0 rounded object-contain bg-black/40"
                          />
                        ) : (
                          <Heart
                            className={'mt-0.5 h-4 w-4 shrink-0 ' + (s.picked ? 'text-[#ff5c1f]' : 'text-zinc-700')}
                            fill={s.picked ? 'currentColor' : 'none'}
                          />
                        )}

                        <div className="min-w-0 flex-1">
                          <p className="flex items-center gap-2 truncate text-sm text-zinc-200">
                            {s.thumbnail_path && (
                              <Heart
                                className={'h-3.5 w-3.5 shrink-0 ' + (s.picked ? 'text-[#ff5c1f]' : 'text-zinc-700')}
                                fill={s.picked ? 'currentColor' : 'none'}
                              />
                            )}
                            {s.filename}
                          </p>
                          {s.comment && <p className="mt-1 text-sm text-zinc-300">“{s.comment}”</p>}
                          {!s.picked && <p className="mt-0.5 text-[11px] text-zinc-600">note only — not picked</p>}
                          <p className="mt-0.5 flex items-center gap-2 text-xs text-zinc-600">
                            {s.viewer_name && <span>— {s.viewer_name}</span>}
                            {s.folder_name && <span className="font-mono">{s.folder_name}</span>}
                          </p>
                        </div>

                        <ExternalLink className="mt-1 h-3.5 w-3.5 shrink-0 text-zinc-700 transition group-hover:text-[#ff5c1f]" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
