import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams } from 'react-router-dom';
import PhoneUpload from '../components/shared/PhoneUpload';
import { Heart, Download, Lock, X, ChevronLeft, ChevronRight, Check, Clock, MessageSquare, CheckSquare, Square, Package } from 'lucide-react';

/**
 * The public face of the library - what a client or agent sees when you send
 * them a link. No account, no sign-up, nothing to install.
 *
 * Everything is fetched through /api/public/share/<token>, which resolves the
 * whole set server-side. The page never asks for a clip by id and hopes; ids
 * it has not been given simply 404.
 */
export default function SharePage() {
  const { token } = useParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [needsPassword, setNeedsPassword] = useState(false);
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(true);
  const [picks, setPicks] = useState({});
  const [openIndex, setOpenIndex] = useState(null);
  const [viewerName, setViewerName] = useState(
    () => { try { return localStorage.getItem('share_viewer_name') || ''; } catch { return ''; } }
  );
  const [comment, setComment] = useState('');
  const [notes, setNotes] = useState({});        // video id -> saved note
  const [noteDraft, setNoteDraft] = useState({}); // video id -> being typed
  const [noteOpen, setNoteOpen] = useState(null); // which card has its note box open
  const [savedFlash, setSavedFlash] = useState(null);

  // Multi-select for batch download. Separate from "picked" on purpose: a
  // client may want to download things they did not choose, and choose things
  // they do not want to download.
  const [selected, setSelected] = useState(() => new Set());
  const [lastClicked, setLastClicked] = useState(null);
  const [marquee, setMarquee] = useState(null);   // drag-rectangle state
  const gridRef = useRef(null);

  // Most of these links are opened on a phone, from WhatsApp. A drag-rectangle
  // is meaningless there - worse, starting one swallows the scroll - so touch
  // gets a tap-to-select mode with the checkboxes always visible instead.
  const [touch] = useState(() => {
    if (typeof window === 'undefined') return false;
    return window.matchMedia
      ? window.matchMedia('(pointer: coarse)').matches
      : 'ontouchstart' in window;
  });
  const [selectMode, setSelectMode] = useState(false);

  const qs = password ? `?password=${encodeURIComponent(password)}` : '';

  const load = useCallback(async (pw) => {
    setLoading(true);
    setError(null);
    try {
      const q = pw ? `?password=${encodeURIComponent(pw)}` : '';
      const res = await fetch(`/api/public/share/${token}${q}`);
      if (res.status === 401) { setNeedsPassword(true); setLoading(false); return; }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || 'This link is not available');
        setLoading(false);
        return;
      }
      setData(await res.json());
      setNeedsPassword(false);
    } catch {
      setError('Could not reach the server');
    }
    setLoading(false);
  }, [token]);

  useEffect(() => { load(''); }, [load]);

  const togglePick = async (video, picked) => {
    setPicks((p) => ({ ...p, [video.id]: picked }));   // optimistic
    try {
      await fetch(`/api/public/share/${token}/select`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_id: video.id, picked, password: password || undefined,
                               viewer_name: viewerName || undefined }),
      });
    } catch {
      setPicks((p) => ({ ...p, [video.id]: !picked }));  // put it back
    }
  };

  /**
   * Save a note against a clip.
   *
   * Deliberately keeps the pick state as it is rather than defaulting to
   * picked - "the lighting is off in this one" is a perfectly normal note to
   * leave on a clip you are NOT choosing, and silently hearting it would put
   * words in the client's mouth.
   */
  const saveNote = async (videoId, text) => {
    const clean = (text ?? '').trim();
    setNotes((n) => ({ ...n, [videoId]: clean }));
    try {
      await fetch(`/api/public/share/${token}/select`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_id: videoId,
          picked: picks[videoId] === true,
          comment: clean,
          password: password || undefined,
          viewer_name: viewerName || undefined,
        }),
      });
      setSavedFlash(videoId);
      setTimeout(() => setSavedFlash((v) => (v === videoId ? null : v)), 1800);
    } catch { /* keep the text on screen so nothing typed is lost */ }
  };

  const sendComment = async (video) => {
    const text = comment.trim();
    if (!text) return;
    setComment('');
    await saveNote(video.id, text);
  };

  const videosList = data?.videos || [];

  const toggleSelect = (id, index, e) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (e?.shiftKey && lastClicked != null) {
        // Range select, the way every file manager behaves.
        const [a, b] = [lastClicked, index].sort((x, y) => x - y);
        for (let i = a; i <= b; i++) next.add(videosList[i].id);
      } else if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
    setLastClicked(index);
  };

  const selectAll = () => setSelected(new Set(videosList.map((v) => v.id)));
  const clearSelection = () => setSelected(new Set());

  // Drag a box across the grid to select. Pointer events rather than mouse
  // events so a trackpad or touchscreen behaves the same.
  const onGridPointerDown = (e) => {
    if (touch || e.pointerType === 'touch' || e.pointerType === 'pen') return;
    if (e.button !== 0) return;
    if (e.target.closest('button, a, input, textarea, figure')) return;  // let controls work
    const rect = gridRef.current?.getBoundingClientRect();
    if (!rect) return;
    setMarquee({ x0: e.clientX, y0: e.clientY, x1: e.clientX, y1: e.clientY, additive: e.ctrlKey || e.metaKey });
  };

  useEffect(() => {
    if (!marquee) return undefined;
    const move = (e) => setMarquee((m) => (m ? { ...m, x1: e.clientX, y1: e.clientY } : m));
    const up = () => {
      setMarquee((m) => {
        if (!m) return null;
        const box = {
          left: Math.min(m.x0, m.x1), right: Math.max(m.x0, m.x1),
          top: Math.min(m.y0, m.y1), bottom: Math.max(m.y0, m.y1),
        };
        // A click, not a drag - leave the selection alone.
        if (box.right - box.left < 6 && box.bottom - box.top < 6) return null;
        const hits = new Set(m.additive ? selected : []);
        gridRef.current?.querySelectorAll('[data-vid]').forEach((el) => {
          const r = el.getBoundingClientRect();
          const overlaps = r.left < box.right && r.right > box.left
                        && r.top < box.bottom && r.bottom > box.top;
          if (overlaps) hits.add(Number(el.getAttribute('data-vid')));
        });
        setSelected(hits);
        return null;
      });
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
  }, [marquee, selected]);

  const fileUrl = (id) =>
    `/api/public/share/${token}/stream/${id}?download=true`
    + (password ? `&password=${encodeURIComponent(password)}` : '');

  const downloadZip = (onlySelected) => {
    const params = new URLSearchParams();
    if (onlySelected && selected.size) params.set('ids', [...selected].join(','));
    if (password) params.set('password', password);
    const qs = params.toString();
    window.location.href = `/api/public/share/${token}/download-zip${qs ? `?${qs}` : ''}`;
  };

  if (loading) {
    return <Shell><p className="text-zinc-400">Loading…</p></Shell>;
  }

  if (needsPassword) {
    return (
      <Shell>
        <div className="w-full max-w-sm">
          <div className="mb-6 flex items-center gap-3 text-zinc-300">
            <Lock className="h-5 w-5 text-accent" />
            <h1 className="text-lg font-medium">This link is password protected</h1>
          </div>
          <form onSubmit={(e) => { e.preventDefault(); load(password); }}>
            <input
              type="password"
              autoFocus
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Password"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-zinc-100 outline-none focus:border-accent"
            />
            <button type="submit" className="mt-3 w-full rounded-lg bg-accent px-4 py-2 font-medium text-accent-foreground transition hover:bg-accent-hi">
              Open
            </button>
          </form>
        </div>
      </Shell>
    );
  }

  if (error) {
    return <Shell><p className="text-zinc-400">{error}</p></Shell>;
  }

  const videos = data?.videos || [];
  const pickedCount = Object.values(picks).filter(Boolean).length;
  const open = openIndex != null ? videos[openIndex] : null;

  return (
    <div className="min-h-screen bg-[#0B0B0D] text-zinc-100">
      <header className="border-b border-white/10 px-6 py-5">
        <div className="mx-auto flex max-w-7xl flex-wrap items-baseline justify-between gap-3">
          <div>
            <h1 className="text-xl font-medium">{data?.title}</h1>
            {data?.message && <p className="mt-1 text-sm text-zinc-400">{data.message}</p>}
          </div>
          <div className="flex items-center gap-4 font-mono text-xs text-zinc-500">
            <span>{data?.count} {data?.count === 1 ? 'clip' : 'clips'}</span>
            {data?.allow_selects && pickedCount > 0 && (
              <span className="text-accent">{pickedCount} picked</span>
            )}
            {data?.expires_at && (
              <span className="flex items-center gap-1">
                <Clock className="h-3 w-3" />
                expires {new Date(data.expires_at).toLocaleDateString()}
              </span>
            )}
          </div>
        </div>
      </header>

      {data?.allow_selects && (
        <div className="border-b border-white/10 bg-white/[0.02] px-6 py-3">
          <div className="mx-auto flex max-w-7xl items-center gap-3 text-sm">
            <span className="text-zinc-500">Your name (so we know whose picks these are):</span>
            <input
              value={viewerName}
              onChange={(e) => {
                setViewerName(e.target.value);
                try { localStorage.setItem('share_viewer_name', e.target.value); } catch { /* private mode */ }
              }}
              placeholder="optional"
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-200 outline-none focus:border-accent"
            />
          </div>
        </div>
      )}

      <main className="mx-auto max-w-7xl px-3 py-4 sm:px-6 sm:py-6">
        {data?.allow_upload && (
          <div className="mb-5 sm:max-w-md">
            <PhoneUpload
              token={token}
              password={password}
              folderName={data.upload_folder}
              onDone={() => load(password)}
            />
          </div>
        )}

        <div
          ref={gridRef}
          onPointerDown={onGridPointerDown}
          className="grid grid-cols-2 gap-2.5 select-none sm:grid-cols-3 sm:gap-4 lg:grid-cols-4"
        >
          {videos.map((v, i) => (
            <figure
              key={v.id}
              data-vid={v.id}
              className={
                'group relative overflow-hidden rounded-lg border bg-black/40 transition ' +
                (selected.has(v.id)
                  ? 'border-sky-400 ring-2 ring-sky-400/50'
                  : 'border-white/10')
              }
            >
              <button
                type="button"
                onClick={(e) => {
                  if (selectMode && data?.allow_download) { toggleSelect(v.id, i, e); return; }
                  setOpenIndex(i);
                }}
                className="block aspect-video w-full"
                title={v.filename}
              >
                <img
                  src={`/api/public/share/${token}/thumb/${v.id}${qs}`}
                  alt={v.filename}
                  loading="lazy"
                  className="h-full w-full object-contain"
                  onError={(e) => { e.currentTarget.style.visibility = 'hidden'; }}
                />
              </button>

              {data?.allow_download && (
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); toggleSelect(v.id, i, e); }}
                  className={
                    'absolute left-1.5 top-1.5 z-10 rounded p-2 transition sm:left-2 sm:top-2 sm:p-1.5 ' +
                    (selected.has(v.id)
                      ? 'bg-sky-500 text-white'
                      : selectMode
                        ? 'bg-black/60 text-white/80'
                        : 'bg-black/60 text-white/60 opacity-0 group-hover:opacity-100 hover:text-white')
                  }
                  title={selected.has(v.id) ? 'Selected' : 'Select (shift-click for a range)'}
                >
                  {selected.has(v.id)
                    ? <CheckSquare className="h-4 w-4" />
                    : <Square className="h-4 w-4" />}
                </button>
              )}

              {data?.allow_download && (
                <a
                  href={fileUrl(v.id)}
                  download={v.filename}
                  onClick={(e) => e.stopPropagation()}
                  title={`Save ${v.filename}`}
                  className="absolute bottom-1.5 left-1.5 z-10 rounded bg-black/60 p-2 text-white/70 transition hover:bg-black/80 hover:text-white sm:opacity-0 sm:group-hover:opacity-100"
                >
                  <Download className="h-4 w-4" />
                </a>
              )}

              {data?.allow_selects && (
                <button
                  type="button"
                  onClick={() => togglePick(v, !picks[v.id])}
                  className={
                    'absolute right-2 top-2 rounded-full p-2 transition ' +
                    (picks[v.id] ? 'bg-accent text-accent-foreground' : 'bg-black/60 text-white/70 hover:text-white')
                  }
                  title={picks[v.id] ? 'Picked' : 'Pick this one'}
                >
                  <Heart className="h-4 w-4" fill={picks[v.id] ? 'currentColor' : 'none'} />
                </button>
              )}

              <figcaption className="px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-xs text-zinc-300" title={v.filename}>{v.filename}</span>
                  <span className="shrink-0 font-mono text-[10px] text-zinc-500">{v.file_size_formatted}</span>
                </div>

                {data?.allow_selects && (
                  <>
                    <button
                      type="button"
                      onClick={() => {
                        setNoteOpen(noteOpen === v.id ? null : v.id);
                        setNoteDraft((d) => ({ ...d, [v.id]: d[v.id] ?? notes[v.id] ?? '' }));
                      }}
                      className={
                        'mt-1.5 flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left text-[11px] transition ' +
                        (notes[v.id]
                          ? 'text-accent hover:bg-white/5'
                          : 'text-zinc-500 hover:bg-white/5 hover:text-zinc-300')
                      }
                    >
                      <MessageSquare className="h-3 w-3 shrink-0" />
                      <span className="truncate">
                        {savedFlash === v.id
                          ? 'Saved'
                          : notes[v.id] || 'Add a note'}
                      </span>
                    </button>

                    {noteOpen === v.id && (
                      <div className="mt-1">
                        <textarea
                          autoFocus
                          rows={2}
                          value={noteDraft[v.id] ?? ''}
                          onChange={(e) => setNoteDraft((d) => ({ ...d, [v.id]: e.target.value }))}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                              e.preventDefault();
                              saveNote(v.id, noteDraft[v.id]);
                              setNoteOpen(null);
                            }
                            if (e.key === 'Escape') setNoteOpen(null);
                          }}
                          placeholder="What do you think of this one?"
                          className="w-full resize-none rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-200 outline-none focus:border-accent"
                        />
                        <div className="mt-1 flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => { saveNote(v.id, noteDraft[v.id]); setNoteOpen(null); }}
                            className="rounded bg-accent px-2 py-0.5 text-[10px] font-medium text-accent-foreground hover:bg-accent-hi"
                          >
                            Save
                          </button>
                          <button
                            type="button"
                            onClick={() => setNoteOpen(null)}
                            className="text-[10px] text-zinc-500 hover:text-zinc-300"
                          >
                            Cancel
                          </button>
                          <span className="ml-auto text-[9px] text-zinc-600">Enter to save</span>
                        </div>
                      </div>
                    )}
                  </>
                )}
              </figcaption>
            </figure>
          ))}
        </div>

        {videos.length === 0 && (
          <p className="py-16 text-center text-zinc-500">Nothing in this share yet.</p>
        )}
      </main>

      {marquee && (
        <div
          className="pointer-events-none fixed z-40 rounded border border-sky-400 bg-sky-400/10"
          style={{
            left: Math.min(marquee.x0, marquee.x1),
            top: Math.min(marquee.y0, marquee.y1),
            width: Math.abs(marquee.x1 - marquee.x0),
            height: Math.abs(marquee.y1 - marquee.y0),
          }}
        />
      )}

      {data?.allow_download && (
        <div className="sticky bottom-0 z-30 border-t border-white/10 bg-[#0B0B0D]/95 px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur sm:px-6">
          <div className="mx-auto flex max-w-7xl flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center sm:gap-3">
            {selected.size > 0 ? (
              <div className="flex items-center gap-2 sm:gap-3">
                <span className="font-mono text-xs text-sky-400">{selected.size} selected</span>
                <button
                  onClick={() => downloadZip(true)}
                  className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-sky-500 px-4 py-2.5 text-sm font-medium text-black transition hover:bg-sky-400 sm:flex-none sm:py-2"
                >
                  <Package className="h-4 w-4" /> Download {selected.size}
                </button>
                <button onClick={clearSelection} className="px-2 py-2 text-xs text-zinc-500 hover:text-zinc-300">
                  Clear
                </button>
              </div>
            ) : (
              <span className="hidden text-xs text-zinc-500 sm:inline">
                Drag a box, or shift-click, to select several.
              </span>
            )}

            <div className="flex items-center gap-2 sm:ml-auto sm:gap-3">
              {touch && (
                <button
                  onClick={() => { setSelectMode((s) => !s); if (selectMode) clearSelection(); }}
                  className={
                    'rounded-lg px-3 py-2.5 text-sm transition ' +
                    (selectMode ? 'bg-sky-500 text-black' : 'border border-zinc-700 text-zinc-200')
                  }
                >
                  {selectMode ? 'Done' : 'Select'}
                </button>
              )}
              <button onClick={selectAll} className="px-2 py-2 text-xs text-zinc-400 hover:text-zinc-100">
                Select all
              </button>
              <button
                onClick={() => downloadZip(false)}
                className="flex flex-1 items-center justify-center gap-2 rounded-lg border border-zinc-700 px-4 py-2.5 text-sm text-zinc-200 transition hover:bg-zinc-800 sm:flex-none sm:py-2"
              >
                <Package className="h-4 w-4" /> All ({videos.length})
              </button>
            </div>
          </div>
          {touch && (
            <p className="mx-auto mt-2 max-w-7xl text-[11px] leading-snug text-zinc-600">
              Tap a clip to save it to your phone. A whole-folder download arrives as a
              zip file in Files, not your photo library.
            </p>
          )}
        </div>
      )}

      {open && (
        <div className="fixed inset-0 z-50 flex flex-col bg-black/95">
          <div className="flex shrink-0 items-center justify-between gap-2 px-3 py-3 pt-[max(0.75rem,env(safe-area-inset-top))] sm:px-4">
            <span className="min-w-0 truncate font-mono text-[11px] text-zinc-400 sm:text-xs">
              {open.filename} · {openIndex + 1} of {videos.length}
            </span>
            <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
              {data?.allow_selects && (
                <button
                  onClick={() => togglePick(open, !picks[open.id])}
                  className={
                    'flex items-center gap-2 rounded-lg px-3 py-1.5 text-xs transition ' +
                    (picks[open.id] ? 'bg-accent text-accent-foreground' : 'bg-white/10 text-white/80 hover:bg-white/20')
                  }
                >
                  <Heart className="h-4 w-4" fill={picks[open.id] ? 'currentColor' : 'none'} />
                  {picks[open.id] ? 'Picked' : 'Pick'}
                </button>
              )}
              {data?.allow_download && (
                <a
                  href={`/api/public/share/${token}/stream/${open.id}?download=true${password ? `&password=${encodeURIComponent(password)}` : ''}`}
                  download={open.filename}
                  className="flex items-center gap-2 rounded-lg bg-white/10 px-3 py-1.5 text-xs text-white/80 transition hover:bg-white/20"
                >
                  <Download className="h-4 w-4" /> <span className="hidden sm:inline">Download</span>
                </a>
              )}
              <button onClick={() => setOpenIndex(null)} className="rounded-lg bg-white/10 p-2 text-white/80 hover:bg-white/20">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          <div className="relative flex min-h-0 flex-1 items-center justify-center px-2 sm:px-14">
            <button
              aria-label="Previous"
              onClick={() => setOpenIndex((i) => (i > 0 ? i - 1 : videos.length - 1))}
              className="absolute left-1 z-10 rounded-full bg-black/60 p-2.5 text-white/70 hover:text-white sm:left-2 sm:p-2"
            >
              <ChevronLeft className="h-6 w-6 sm:h-7 sm:w-7" />
            </button>

            {open.media_type === 'photo' ? (
              <img
                src={`/api/public/share/${token}/stream/${open.id}${qs}`}
                alt={open.filename}
                className="max-h-full max-w-full object-contain"
              />
            ) : (
              <video
                key={open.id}
                src={`/api/public/share/${token}/stream/${open.id}${qs}`}
                controls
                autoPlay
                className="max-h-full max-w-full"
              />
            )}

            <button
              aria-label="Next"
              onClick={() => setOpenIndex((i) => (i < videos.length - 1 ? i + 1 : 0))}
              className="absolute right-1 z-10 rounded-full bg-black/60 p-2.5 text-white/70 hover:text-white sm:right-2 sm:p-2"
            >
              <ChevronRight className="h-6 w-6 sm:h-7 sm:w-7" />
            </button>
          </div>

          {data?.allow_selects && (
            <div className="shrink-0 border-t border-white/10 px-4 py-3">
              <div className="mx-auto flex max-w-2xl gap-2">
                <input
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') sendComment(open); }}
                  placeholder={notes[open.id] ? `Note: ${notes[open.id]}` : 'Leave a note on this clip…'}
                  className="flex-1 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-accent"
                />
                <button
                  onClick={() => sendComment(open)}
                  className="flex items-center gap-2 rounded-lg bg-white/10 px-4 py-2 text-sm text-white/80 hover:bg-white/20"
                >
                  <Check className="h-4 w-4" /> Send
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Shell({ children }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-[#0B0B0D] px-6 text-zinc-100">
      {children}
    </div>
  );
}
