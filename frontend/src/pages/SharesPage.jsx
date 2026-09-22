import { useState, useEffect, useCallback } from 'react';
import { Link2, Copy, Check, Trash2, Heart, Eye, Clock, Download, RefreshCw, ExternalLink, Inbox, QrCode } from 'lucide-react';
import { apiCall } from '../lib/api';
import { useNavigate } from 'react-router-dom';
import IntakePanel from '../components/shared/IntakePanel';
import ShareDialog from '../components/shared/ShareDialog';
import QRCode from 'qrcode';

export default function SharesPage() {
  const navigate = useNavigate();
  const [shares, setShares] = useState([]);
  const [loading, setLoading] = useState(true);
  const [openId, setOpenId] = useState(null);
  const [selects, setSelects] = useState(null);
  const [intakeShare, setIntakeShare] = useState(null);
  const [qrShare, setQrShare] = useState(null);
  const [creating, setCreating] = useState(false);
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
            <h1 className="text-xl font-medium text-zinc-100">Portals</h1>
            <p className="mt-1 text-sm text-zinc-500">
              Links you have sent out, and what came back.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={load} className="flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 transition hover:border-zinc-600 hover:bg-zinc-800">
              <RefreshCw className="h-4 w-4" /> Refresh
            </button>
            <button
              onClick={() => setCreating(true)}
              className="flex items-center gap-2 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-foreground transition hover:bg-accent-hi"
            >
              <Link2 className="h-4 w-4" /> New portal
            </button>
          </div>
        </div>

        {loading ? (
          <p className="text-zinc-500">Loading…</p>
        ) : shares.length === 0 ? (
          <div className="rounded-xl border border-dashed border-zinc-800 py-16 text-center">
            <Link2 className="mx-auto mb-3 h-8 w-8 text-zinc-700" />
            <p className="text-zinc-400">No portals yet.</p>
            <p className="mt-1 text-sm text-zinc-600">
              Make one with New portal above, or right-click a folder in the sidebar.
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
                          <span className="flex items-center gap-1 text-accent">
                            <Heart className="h-3 w-3" fill="currentColor" />{sh.selects} picked
                          </span>
                        )}
                        {sh.kind === 'receive' && (
                          <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-300">receiving</span>
                        )}
                        {sh.kind === 'send' && sh.allow_download && !sh.allow_zip && (
                          <span className="text-zinc-500">single files only</span>
                        )}
                        {sh.confirmed_at && (
                          <span className="text-emerald-400">
                            client finished {new Date(sh.confirmed_at).toLocaleDateString()}
                          </span>
                        )}
                        {sh.allow_upload && (
                          <span className="flex items-center gap-1 text-emerald-400">
                            <Inbox className="h-3 w-3" />
                            {sh.intake_waiting > 0
                              ? `${sh.intake_waiting} waiting`
                              : 'accepts uploads'}
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
                      {sh.allow_upload && (
                        <button
                          onClick={() => setIntakeShare(sh)}
                          className={
                            'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs transition ' +
                            (sh.intake_waiting > 0
                              ? 'bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25'
                              : 'border border-zinc-700 text-zinc-400 hover:bg-zinc-800')
                          }
                        >
                          <Inbox className="h-3.5 w-3.5" />
                          {sh.intake_waiting > 0 ? `Review ${sh.intake_waiting}` : 'Inbox'}
                        </button>
                      )}
                      {!dead && (
                        <button onClick={() => setQrShare(sh)} title="Show QR code"
                                className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200">
                          <QrCode className="h-3.5 w-3.5" />
                        </button>
                      )}
                      {sh.selects > 0 && (
                        <button onClick={() => openSelects(sh.id)}
                                className="rounded-lg bg-accent/15 px-3 py-1.5 text-xs text-accent hover:bg-accent/25">
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

      {creating && (
        <ShareDialog
          folderId={null}
          folderName={null}
          videoIds={null}
          onClose={() => { setCreating(false); load(); }}
        />
      )}

      {intakeShare && (
        <IntakePanel
          share={intakeShare}
          onClose={() => setIntakeShare(null)}
          onFiled={load}
        />
      )}

      {qrShare && <QrModal share={qrShare} onClose={() => setQrShare(null)} />}

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
                        className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-accent-foreground hover:bg-accent-hi">
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
                            ? 'border-zinc-800 hover:border-accent/60 hover:bg-zinc-800/40'
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
                            className={'mt-0.5 h-4 w-4 shrink-0 ' + (s.picked ? 'text-accent' : 'text-zinc-700')}
                            fill={s.picked ? 'currentColor' : 'none'}
                          />
                        )}

                        <div className="min-w-0 flex-1">
                          <p className="flex items-center gap-2 truncate text-sm text-zinc-200">
                            {s.thumbnail_path && (
                              <Heart
                                className={'h-3.5 w-3.5 shrink-0 ' + (s.picked ? 'text-accent' : 'text-zinc-700')}
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

                        <ExternalLink className="mt-1 h-3.5 w-3.5 shrink-0 text-zinc-700 transition group-hover:text-accent" />
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

/**
 * The link as a QR code, for pointing a phone at. Generated in the browser -
 * sending the URL to a QR service would hand a stranger a working link to
 * the footage.
 */
function QrModal({ share, onClose }) {
  const [src, setSrc] = useState(null);
  const url = `${window.location.origin}${share.url}`;

  useEffect(() => {
    let alive = true;
    QRCode.toDataURL(url, { margin: 1, width: 420, color: { dark: '#0b0b0d', light: '#ffffff' } })
      .then((d) => { if (alive) setSrc(d); })
      .catch(() => { if (alive) setSrc(null); });
    return () => { alive = false; };
  }, [url]);

  return (
    <div className="animate-shade-in fixed inset-0 z-[70] flex items-center justify-center bg-black/70 p-4"
         onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="animate-pop-in w-full max-w-xs rounded-xl border border-zinc-700 bg-zinc-900 p-5 text-center shadow-2xl">
        <h2 className="mb-1 truncate text-sm font-medium text-zinc-100">{share.title || 'Link'}</h2>
        <p className="mb-4 text-xs text-zinc-500">
          {share.allow_upload
            ? 'Scan with a phone to send photos and clips in.'
            : 'Scan with a phone to open this link.'}
        </p>
        {src
          ? <img src={src} alt="QR code" className="mx-auto w-full rounded-lg bg-white p-2" />
          : <p className="py-10 text-xs text-zinc-600">Could not draw the code.</p>}
        <button onClick={onClose}
                className="mt-4 w-full rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
          Close
        </button>
      </div>
    </div>
  );
}
