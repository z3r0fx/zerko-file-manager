import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  X, Download, Loader2, ArrowLeft, ArrowRight, Check, AlertTriangle, Crop, ChevronLeft, ChevronRight,
  ImageDown, ArrowUpToLine, ArrowDownToLine, Rows3,
} from 'lucide-react';
import { cn } from '../../lib/utils';
import { apiCall, API_BASE } from '../../lib/api';
import { RATIO, slideAxis, objectPosition, ratioFromResolution } from '../../lib/framing';
import { ensureCrops, getCrop, saveCrops } from '../../lib/photoCrops';

/**
 * Generate proxy: 16:9 copies of photos, zipped, for listing sites.
 *
 *   1. Choose   - which photos, what size, what quality
 *   2. Framing  - a 4:3 photo does not fit 16:9: drag the bright window over the
 *                 part to keep (the rest is cut off)
 *   3. Export   - progress, then one ZIP to download
 *
 * Originals are never changed. The framing chosen here is remembered, and the
 * library grid and the viewer use it too.
 */

const SIZES = [
  { key: '1920', w: 1920, label: '1920 × 1080', hint: 'Full HD - a good default for listings' },
  { key: '1280', w: 1280, label: '1280 × 720', hint: 'HD - smaller files' },
  { key: '1024', w: 1024, label: '1024 × 576', hint: 'Small' },
  { key: '800', w: 800, label: '800 × 450', hint: 'Thumbnail-sized' },
  { key: 'custom', w: null, label: 'Custom width', hint: 'Height follows at 16:9' },
  { key: 'native', w: 0, label: 'Native', hint: "Original resolution, cropped to 16:9 - no resizing" },
];

const token = () => { try { return localStorage.getItem('token') || ''; } catch { return ''; } };
const thumbOf = (p) => (p.thumbnail_path
  ? (p.thumbnail_path.startsWith('http') ? p.thumbnail_path : `${API_BASE}${p.thumbnail_path}`) : null);
const bigOf = (p) => `${API_BASE}/api/photo-preview/${p.id}?w=1400&token=${token()}`;

function ratioName(r) {
  if (!r) return '';
  const known = [[4 / 3, '4:3'], [3 / 2, '3:2'], [16 / 9, '16:9'], [5 / 4, '5:4'], [1, '1:1'], [3 / 4, '3:4 portrait'], [2 / 3, '2:3 portrait'], [2, '2:1'], [3, '3:1 panorama']];
  const hit = known.find(([v]) => Math.abs(v - r) < 0.02);
  return hit ? hit[1] : `${r.toFixed(2)}:1`;
}

function flattenFolders(tree, depth = 0, out = []) {
  (tree || []).forEach((n) => {
    const count = n.total_by_type ? (n.total_by_type.photo || 0) : (n.total_count || 0);
    if (count > 0 || !n.total_by_type) out.push({ id: n.id, name: n.name, depth, count });
    flattenFolders(n.children, depth + 1, out);
  });
  return out;
}

// ---------------------------------------------------------------- framing
function FramingStage({ photo, ratio, y, onChange }) {
  const boxRef = useRef(null);
  const [avail, setAvail] = useState({ w: 640, h: 360 });
  const dragRef = useRef(null);

  useEffect(() => {
    const el = boxRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(() => setAvail({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setAvail({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const r = ratio || 4 / 3;
  const axis = slideAxis(r);
  const Pw = Math.max(40, Math.min(avail.w, avail.h * r));
  const Ph = Pw / r;
  // the bright window: 16:9, sliding along the axis where the photo has room
  const win = axis === 'x'
    ? { w: Ph * RATIO, h: Ph }
    : { w: Pw, h: Pw / RATIO };
  const slack = axis === 'x' ? Pw - win.w : axis === 'y' ? Ph - win.h : 0;
  const off = slack * y;
  const winStyle = axis === 'x' ? { left: off, top: 0 } : axis === 'y' ? { left: 0, top: off } : { left: 0, top: 0 };
  const thumb = thumbOf(photo);
  // show the small thumbnail at once, swap to the sharp preview when it arrives
  const [src, setSrc] = useState(thumb || bigOf(photo));
  useEffect(() => {
    const big = bigOf(photo);
    setSrc(thumb || big);
    if (!thumb) return undefined;
    const im = new Image();
    im.onload = () => setSrc(big);
    im.src = big;
    return () => { im.onload = null; };
  }, [photo.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const posOf = (e) => {
    const b = e.currentTarget.getBoundingClientRect();
    return axis === 'x' ? e.clientX - b.left : e.clientY - b.top;
  };
  const down = (e) => {
    if (!axis || slack < 1) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    const p = posOf(e);
    const len = axis === 'x' ? win.w : win.h;
    const inside = p >= off && p <= off + len;
    dragRef.current = { grab: inside ? p - off : len / 2 };     // click outside: window jumps to you
    onChange(Math.max(0, Math.min(1, (p - dragRef.current.grab) / slack)));
  };
  const move = (e) => {
    if (!dragRef.current) return;
    onChange(Math.max(0, Math.min(1, (posOf(e) - dragRef.current.grab) / slack)));
  };
  const up = (e) => { dragRef.current = null; e.currentTarget.releasePointerCapture?.(e.pointerId); };

  return (
    <div ref={boxRef} className="flex h-full min-h-0 w-full items-center justify-center">
      <div
        className={cn('relative shrink-0 select-none overflow-hidden rounded-md bg-zinc-950',
          axis === 'y' && 'cursor-ns-resize', axis === 'x' && 'cursor-ew-resize')}
        style={{ width: Pw, height: Ph, touchAction: 'none' }}
        onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}
      >
        {/* the whole photo, dimmed: what is cut off */}
        <img src={src} alt="" draggable={false} className="pointer-events-none absolute inset-0 h-full w-full object-fill" />
        <div className="pointer-events-none absolute inset-0 bg-black/65" />
        {/* the window: what the 16:9 copy keeps */}
        <div className="pointer-events-none absolute overflow-hidden ring-2 ring-accent" style={{ width: win.w, height: win.h, ...winStyle }}>
          <img src={src} alt="" draggable={false}
               className="absolute max-w-none object-fill"
               style={{ width: Pw, height: Ph, left: -(winStyle.left || 0), top: -(winStyle.top || 0) }} />
          <span className="absolute left-2 top-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-white">16:9</span>
          {/* thirds grid, like a camera's framing guide */}
          <div className="absolute inset-0 opacity-40" style={{
            backgroundImage: 'linear-gradient(to right, transparent 33.2%, rgba(255,255,255,.55) 33.3%, transparent 33.6%, transparent 66.5%, rgba(255,255,255,.55) 66.6%, transparent 66.9%), linear-gradient(to bottom, transparent 33.2%, rgba(255,255,255,.55) 33.3%, transparent 33.6%, transparent 66.5%, rgba(255,255,255,.55) 66.6%, transparent 66.9%)',
          }} />
        </div>
        {!axis && (
          <span className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full bg-black/70 px-3 py-1 text-xs text-zinc-200">
            Already 16:9 - nothing is cut off
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- dialog
export default function PhotoProxyDialog({ onClose, selectedPhotos = [], viewPhotos = [], folderTree = [], currentFolderId = null }) {
  const [step, setStep] = useState('setup');            // setup | frame | run
  const defaultSource = selectedPhotos.length ? 'selected' : (viewPhotos.length ? 'view' : 'folder');
  const [source, setSource] = useState(defaultSource);
  const folders = useMemo(() => flattenFolders(folderTree), [folderTree]);
  const [folderId, setFolderId] = useState(() => (folders.find((f) => f.id === currentFolderId) || folders[0] || {}).id ?? null);
  const [subfolders, setSubfolders] = useState(true);
  const [folderPhotos, setFolderPhotos] = useState(null);
  const [loadingFolder, setLoadingFolder] = useState(false);
  const [loadError, setLoadError] = useState('');

  const [sizeKey, setSizeKey] = useState('1920');
  const [customW, setCustomW] = useState(1600);
  const [quality, setQuality] = useState(85);
  const [limitOn, setLimitOn] = useState(false);
  const [maxKb, setMaxKb] = useState(500);
  const [numbered, setNumbered] = useState(false);
  const [prefix, setPrefix] = useState('photo');

  const [photos, setPhotos] = useState([]);              // resolved list for steps 2-3
  const [crops, setCrops] = useState({});                // id -> y, this session
  const [idx, setIdx] = useState(0);
  const [ratios, setRatios] = useState({});
  const [saveNote, setSaveNote] = useState('');

  const [job, setJob] = useState(null);
  const [runError, setRunError] = useState('');
  const pollRef = useRef(null);

  useEffect(() => { ensureCrops(); }, []);
  useEffect(() => () => clearTimeout(pollRef.current), []);
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && step !== 'frame') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, step]);

  // photos of a chosen folder
  useEffect(() => {
    if (source !== 'folder' || folderId == null) { setFolderPhotos(null); return undefined; }
    let dead = false;
    setLoadingFolder(true); setLoadError('');
    apiCall(`/api/videos?folder_id=${folderId}&include_subfolders=${subfolders}&media_type=photo`)
      .then((list) => { if (!dead) setFolderPhotos((list || []).filter((v) => v.media_type === 'photo')); })
      .catch(() => { if (!dead) { setFolderPhotos([]); setLoadError('Could not read that folder.'); } })
      .finally(() => { if (!dead) setLoadingFolder(false); });
    return () => { dead = true; };
  }, [source, folderId, subfolders]);

  const chosen = source === 'selected' ? selectedPhotos : source === 'view' ? viewPhotos : (folderPhotos || []);
  const width = sizeKey === 'native' ? 0 : sizeKey === 'custom' ? Math.round(Number(customW) || 0) : Number(sizeKey);
  const widthOk = sizeKey === 'native' || (width >= 160 && width <= 8000);
  const outLabel = sizeKey === 'native' ? 'original resolution' : widthOk ? `${width} × ${Math.round(width * 9 / 16)}` : '-';

  const yOf = useCallback((id) => (crops[id] !== undefined ? crops[id] : getCrop(id)), [crops]);
  const cur = photos[idx];
  const curRatio = cur ? (ratios[cur.id] || ratioFromResolution(cur.resolution)) : null;
  const curAxis = slideAxis(curRatio || 4 / 3);

  // learn the current photo's shape as soon as its thumbnail is in
  useEffect(() => {
    if (step !== 'frame' || !cur || ratios[cur.id]) return undefined;
    const t = thumbOf(cur);
    if (!t) return undefined;
    const im = new Image();
    im.onload = () => setRatios((r) => ({ ...r, [cur.id]: im.naturalWidth / (im.naturalHeight || 1) }));
    im.src = t;
    return () => { im.onload = null; };
  }, [step, cur, ratios]);

  const setY = (id, v) => setCrops((c) => ({ ...c, [id]: Math.max(0, Math.min(1, v)) }));

  // arrows nudge along the way the window slides; the other arrows change photo
  useEffect(() => {
    if (step !== 'frame') return undefined;
    const onKey = (e) => {
      if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
      const step1 = e.shiftKey ? 0.1 : 0.02;
      const next = () => setIdx((i) => Math.min(photos.length - 1, i + 1));
      const prev = () => setIdx((i) => Math.max(0, i - 1));
      const k = e.key;
      if (k === 'Escape') { onClose(); return; }
      if (k === 'PageDown' || k === ']') { e.preventDefault(); next(); return; }
      if (k === 'PageUp' || k === '[') { e.preventDefault(); prev(); return; }
      if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(k) || !cur) return;
      e.preventDefault();
      const along = curAxis === 'y' ? ['ArrowUp', 'ArrowDown'] : curAxis === 'x' ? ['ArrowLeft', 'ArrowRight'] : [];
      if (along.includes(k)) setY(cur.id, yOf(cur.id) + (k === along[0] ? -step1 : step1));
      else if (k === 'ArrowRight' || k === 'ArrowDown') next();
      else prev();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [step, cur, curAxis, photos.length, yOf, onClose]);

  const begin = (nextStep) => {
    if (!chosen.length) return;
    setPhotos(chosen);
    setIdx(0);
    setCrops({});
    setStep(nextStep);
    if (nextStep === 'run') startExport(chosen, {});
  };

  const changedCrops = (list, local) => {
    const out = {};
    list.forEach((p) => { if (local[p.id] !== undefined && Math.abs(local[p.id] - getCrop(p.id)) > 0.0005) out[p.id] = local[p.id]; });
    return out;
  };

  const saveFraming = async () => {
    const ch = changedCrops(photos, crops);
    try { await saveCrops(ch); setCrops({}); setSaveNote(Object.keys(ch).length ? `Saved framing for ${Object.keys(ch).length} photo${Object.keys(ch).length === 1 ? '' : 's'}. The library will show it too.` : 'Nothing to save yet.'); }
    catch { setSaveNote('Could not save the framing.'); }
    setTimeout(() => setSaveNote(''), 4000);
  };

  const poll = (id) => {
    clearTimeout(pollRef.current);
    pollRef.current = setTimeout(async () => {
      try {
        const j = await apiCall(`/api/photo-export/jobs/${id}`);
        setJob(j);
        if (j.state === 'running') poll(id);
      } catch (e) { setRunError('Lost track of the export. Generate it again.'); }
    }, 600);
  };

  const startExport = async (list = photos, local = crops) => {
    setStep('run'); setJob(null); setRunError('');
    try {
      const ch = changedCrops(list, local);
      try { await saveCrops(ch); } catch { /* the export still carries them */ }
      const sendCrops = {};
      list.forEach((p) => { sendCrops[p.id] = local[p.id] !== undefined ? local[p.id] : getCrop(p.id); });
      const j = await apiCall('/api/photo-export/generate', {
        method: 'POST',
        body: JSON.stringify({
          ids: list.map((p) => p.id),
          width: width || null,
          quality,
          max_kb: limitOn ? Math.max(20, Math.round(Number(maxKb) || 0)) : null,
          crops: sendCrops,
          numbered,
          prefix: prefix.trim() || 'photo',
        }),
      });
      setJob(j);
      if (j.state === 'running') poll(j.id);
    } catch (e) {
      const m = /"detail":"([^"]+)"/.exec(String(e.message));
      setRunError(m ? m[1] : 'Could not start the export.');
    }
  };

  const applyToAll = () => {
    if (!cur) return;
    const v = yOf(cur.id);
    setCrops(() => Object.fromEntries(photos.map((p) => [p.id, v])));
  };
  const centreAll = () => setCrops(Object.fromEntries(photos.map((p) => [p.id, 0.5])));

  const stepIdx = { setup: 0, frame: 1, run: 2 }[step];
  const pct = job && job.total ? Math.round((job.done / job.total) * 100) : 0;

  return createPortal(
    <div className="fixed inset-0 z-[90] flex items-center justify-center p-3 sm:p-6" role="dialog" aria-modal="true" aria-label="Generate proxy">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-[2px]" onClick={step === 'frame' ? undefined : onClose} />
      <div className="relative flex max-h-[94vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-900 shadow-2xl shadow-black/70">
        {/* header */}
        <div className="flex shrink-0 items-center gap-4 border-b border-zinc-800 px-5 py-3.5">
          <ImageDown className="h-5 w-5 text-accent" />
          <h2 className="text-base font-semibold text-zinc-100">Generate proxy</h2>
          <ol className="ml-2 hidden items-center gap-1 text-xs sm:flex">
            {['Choose', 'Framing', 'Export'].map((label, i) => (
              <li key={label} className="flex items-center gap-1">
                {i > 0 && <ChevronRight className="h-3 w-3 text-zinc-700" />}
                <span className={cn('rounded-full px-2.5 py-1', i === stepIdx ? 'bg-accent/15 font-medium text-accent' : i < stepIdx ? 'text-zinc-400' : 'text-zinc-600')}>
                  {i + 1}. {label}
                </span>
              </li>
            ))}
          </ol>
          <button onClick={onClose} aria-label="Close" className="ml-auto rounded-md p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* ------------------------------------------------ 1. choose */}
        {step === 'setup' && (
          <>
            <div className="grid min-h-0 flex-1 gap-6 overflow-y-auto p-5 md:grid-cols-2">
              <section>
                <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-500">Which photos</h3>
                <div className="space-y-2">
                  {[
                    ['selected', `Selected photos`, selectedPhotos.length, selectedPhotos.length === 0 ? 'Nothing selected in the library' : ''],
                    ['view', 'Every photo in this view', viewPhotos.length, viewPhotos.length === 0 ? 'No photos on screen' : ''],
                    ['folder', 'A folder…', source === 'folder' && folderPhotos ? folderPhotos.length : null, ''],
                  ].map(([key, label, n, disabledWhy]) => (
                    <label key={key} className={cn('flex cursor-pointer items-center gap-3 rounded-xl border px-3.5 py-2.5 transition',
                      source === key ? 'border-accent/60 bg-accent/10' : 'border-zinc-800 hover:border-zinc-700',
                      disabledWhy && 'cursor-not-allowed opacity-45')}>
                      <input type="radio" name="src" className="accent-red-600" checked={source === key} disabled={!!disabledWhy}
                             onChange={() => setSource(key)} />
                      <span className="flex-1 text-sm text-zinc-200">{label}
                        {disabledWhy && <span className="ml-2 text-xs text-zinc-500">{disabledWhy}</span>}
                      </span>
                      {n != null && !disabledWhy && <span className="text-xs tabular-nums text-zinc-500">{n}</span>}
                    </label>
                  ))}
                </div>
                {source === 'folder' && (
                  <div className="mt-3 space-y-2 rounded-xl border border-zinc-800 bg-zinc-950/40 p-3">
                    {folders.length === 0 ? (
                      <p className="text-sm text-zinc-500">No folder has photos yet.</p>
                    ) : (
                      <select value={folderId ?? ''} onChange={(e) => setFolderId(Number(e.target.value))}
                              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-accent">
                        {folders.map((f) => (
                          <option key={f.id} value={f.id}>{' '.repeat(f.depth)}{f.name}  ({f.count})</option>
                        ))}
                      </select>
                    )}
                    <label className="flex cursor-pointer items-center gap-2 text-xs text-zinc-400">
                      <input type="checkbox" className="accent-red-600" checked={subfolders} onChange={(e) => setSubfolders(e.target.checked)} />
                      Include photos in subfolders
                    </label>
                    {loadingFolder && <p className="flex items-center gap-2 text-xs text-zinc-500"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Reading folder…</p>}
                    {loadError && <p className="text-xs text-red-400">{loadError}</p>}
                  </div>
                )}
                <p className="mt-3 text-xs leading-relaxed text-zinc-500">
                  Your originals are never changed. Only photos are exported - videos and audio in the selection are ignored.
                </p>
              </section>

              <section className="space-y-5">
                <div>
                  <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-500">Size (16:9, landscape)</h3>
                  <div className="grid grid-cols-2 gap-2">
                    {SIZES.map((s) => (
                      <button key={s.key} type="button" onClick={() => setSizeKey(s.key)}
                              className={cn('rounded-xl border px-3 py-2 text-left transition',
                                sizeKey === s.key ? 'border-accent/60 bg-accent/10' : 'border-zinc-800 hover:border-zinc-700')}>
                        <div className="text-sm font-medium text-zinc-100">{s.label}</div>
                        <div className="text-[11px] leading-snug text-zinc-500">{s.hint}</div>
                      </button>
                    ))}
                  </div>
                  {sizeKey === 'custom' && (
                    <div className="mt-2 flex items-center gap-2 text-sm text-zinc-300">
                      <input type="number" min={160} max={8000} value={customW} onChange={(e) => setCustomW(e.target.value)}
                             className="w-28 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-zinc-100 outline-none focus:border-accent" />
                      <span>px wide → <span className="text-zinc-100">{outLabel}</span></span>
                      {!widthOk && <span className="text-xs text-red-400">160 – 8000</span>}
                    </div>
                  )}
                  <p className="mt-2 text-xs text-zinc-500">A photo smaller than the size you pick is never enlarged.</p>
                </div>

                <div>
                  <div className="mb-1 flex items-center justify-between">
                    <h3 className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">JPEG quality</h3>
                    <span className="text-sm tabular-nums text-zinc-200">{quality}</span>
                  </div>
                  <input type="range" min={60} max={100} value={quality} onChange={(e) => setQuality(Number(e.target.value))} className="w-full accent-red-600" />
                  <div className="flex justify-between text-[11px] text-zinc-600"><span>smaller files</span><span>best quality</span></div>
                </div>

                <div className="space-y-2">
                  <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
                    <input type="checkbox" className="accent-red-600" checked={limitOn} onChange={(e) => setLimitOn(e.target.checked)} />
                    Keep each photo under
                    <input type="number" min={20} value={maxKb} disabled={!limitOn} onChange={(e) => setMaxKb(e.target.value)}
                           className="w-20 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100 outline-none focus:border-accent disabled:opacity-40" />
                    KB
                  </label>
                  <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
                    <input type="checkbox" className="accent-red-600" checked={numbered} onChange={(e) => setNumbered(e.target.checked)} />
                    Number the files in order
                    <input type="text" value={prefix} disabled={!numbered} onChange={(e) => setPrefix(e.target.value)} maxLength={30}
                           className="w-28 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100 outline-none focus:border-accent disabled:opacity-40" />
                    <span className="text-xs text-zinc-500">{numbered ? `${prefix.trim() || 'photo'}-01.jpg` : ''}</span>
                  </label>
                </div>
              </section>
            </div>
            <div className="flex shrink-0 items-center justify-between gap-3 border-t border-zinc-800 px-5 py-3.5">
              <span className="text-xs text-zinc-500">{chosen.length} photo{chosen.length === 1 ? '' : 's'} · {outLabel}</span>
              <div className="flex gap-2">
                <button disabled={!chosen.length || !widthOk} onClick={() => begin('run')}
                        className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-200 transition hover:bg-zinc-800 disabled:opacity-40"
                        title="Use centred / saved framing and export straight away">
                  Export now
                </button>
                <button disabled={!chosen.length || !widthOk} onClick={() => begin('frame')}
                        className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-accent-foreground transition hover:bg-accent-hi disabled:opacity-40">
                  <Crop className="h-4 w-4" /> Adjust framing <ArrowRight className="h-4 w-4" />
                </button>
              </div>
            </div>
          </>
        )}

        {/* ------------------------------------------------ 2. framing */}
        {step === 'frame' && cur && (
          <>
            <div className="grid min-h-0 flex-1 gap-4 overflow-y-auto p-5 lg:grid-cols-[minmax(0,1fr)_260px]" style={{ minHeight: 0 }}>
              <div className="flex min-w-0 flex-col gap-3">
                <div className="flex items-center gap-2">
                  <button onClick={() => setIdx((i) => Math.max(0, i - 1))} disabled={idx === 0} aria-label="Previous photo"
                          className="rounded-md p-1.5 text-zinc-400 hover:bg-zinc-800 disabled:opacity-30"><ChevronLeft className="h-5 w-5" /></button>
                  <div className="min-w-0 flex-1 text-center">
                    <p className="truncate text-sm font-medium text-zinc-100" title={cur.filename}>{cur.filename}</p>
                    <p className="text-[11px] text-zinc-500">{idx + 1} of {photos.length}{curRatio ? ` · ${ratioName(curRatio)} photo` : ''}</p>
                  </div>
                  <button onClick={() => setIdx((i) => Math.min(photos.length - 1, i + 1))} disabled={idx === photos.length - 1} aria-label="Next photo"
                          className="rounded-md p-1.5 text-zinc-400 hover:bg-zinc-800 disabled:opacity-30"><ChevronRight className="h-5 w-5" /></button>
                </div>
                <div className="h-[44vh] min-h-[240px] rounded-xl border border-zinc-800 bg-zinc-950/60 p-3">
                  <FramingStage photo={cur} ratio={curRatio} y={yOf(cur.id)} onChange={(v) => setY(cur.id, v)} />
                </div>
                <p className="text-center text-xs text-zinc-500">
                  {curAxis === 'y' ? 'Drag the bright frame up or down (or click where you want it). ↑ ↓ nudge, Shift for bigger steps, ← → change photo.'
                    : curAxis === 'x' ? 'This photo is wider than 16:9 - drag the frame left or right. ← → nudge, ↑ ↓ change photo.'
                      : 'This one is already 16:9. Nothing to adjust.'}
                </p>
              </div>

              <aside className="space-y-4">
                <div>
                  <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-500">The result</p>
                  <div className="relative aspect-video w-full overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950">
                    {thumbOf(cur) && (
                      <img src={thumbOf(cur)} alt="" className="h-full w-full object-cover"
                           style={{ objectPosition: objectPosition(curRatio, yOf(cur.id)) }} />
                    )}
                  </div>
                  <p className="mt-1 text-[11px] text-zinc-500">{outLabel} · this is also how the tile looks in the library</p>
                </div>

                {curAxis && (
                  <div>
                    <div className="mb-1 flex items-center justify-between text-xs text-zinc-400">
                      <span>{curAxis === 'y' ? 'Top' : 'Left'}</span>
                      <span className="tabular-nums">{Math.round(yOf(cur.id) * 100)}%</span>
                      <span>{curAxis === 'y' ? 'Bottom' : 'Right'}</span>
                    </div>
                    <input type="range" min={0} max={100} value={Math.round(yOf(cur.id) * 100)}
                           onChange={(e) => setY(cur.id, Number(e.target.value) / 100)} className="w-full accent-red-600" />
                    {curRatio && (() => {
                      const keep = curAxis === 'y' ? (curRatio / RATIO) : (RATIO / curRatio);   // fraction of the photo that stays
                      const cut = Math.max(0, 1 - keep);
                      return (
                        <p className="mt-1 text-[11px] text-zinc-500">
                          Cuts off {Math.round(cut * yOf(cur.id) * 100)}% {curAxis === 'y' ? 'above' : 'on the left'} and {Math.round(cut * (1 - yOf(cur.id)) * 100)}% {curAxis === 'y' ? 'below' : 'on the right'}.
                        </p>
                      );
                    })()}
                    <div className="mt-2 grid grid-cols-3 gap-1.5">
                      {[[0, curAxis === 'y' ? 'Top' : 'Left', ArrowUpToLine], [0.5, 'Centre', Rows3], [1, curAxis === 'y' ? 'Bottom' : 'Right', ArrowDownToLine]].map(([v, label, Icon]) => (
                        <button key={label} onClick={() => setY(cur.id, v)}
                                className="flex items-center justify-center gap-1 rounded-lg border border-zinc-700 px-2 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800">
                          <Icon className={cn('h-3.5 w-3.5', curAxis === 'x' && '-rotate-90')} /> {label}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {curRatio && curRatio < 0.95 && (
                  <p className="flex gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-200">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    A portrait photo keeps only a thin strip at 16:9. Consider leaving it out.
                  </p>
                )}
                <div className="space-y-1.5">
                  <button onClick={applyToAll} className="w-full rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800">
                    Use this position for all {photos.length} photos
                  </button>
                  <button onClick={centreAll} className="w-full rounded-lg border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400 transition hover:bg-zinc-800">
                    Centre every photo
                  </button>
                </div>
              </aside>
            </div>

            {/* filmstrip */}
            <div className="shrink-0 border-t border-zinc-800 bg-zinc-950/40 px-5 py-2.5">
              <div className="flex gap-2 overflow-x-auto pb-1">
                {photos.map((p, i) => {
                  const r = ratios[p.id] || ratioFromResolution(p.resolution);
                  const edited = Math.abs(yOf(p.id) - 0.5) > 0.0005;
                  return (
                    <button key={p.id} type="button" onClick={() => setIdx(i)} title={p.filename}
                            ref={(el) => { if (el && i === idx) el.scrollIntoView({ block: 'nearest', inline: 'center' }); }}
                            className={cn('relative aspect-video h-14 shrink-0 overflow-hidden rounded-md border transition',
                              i === idx ? 'border-accent ring-1 ring-accent' : 'border-zinc-800 opacity-70 hover:opacity-100')}>
                      {thumbOf(p) && <img src={thumbOf(p)} alt="" loading="lazy" className="h-full w-full object-cover"
                                          style={{ objectPosition: objectPosition(r, yOf(p.id)) }} />}
                      {edited && <span className="absolute right-1 top-1 h-2 w-2 rounded-full bg-accent shadow" />}
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="flex shrink-0 items-center justify-between gap-3 border-t border-zinc-800 px-5 py-3.5">
              <button onClick={() => setStep('setup')} className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
                <ArrowLeft className="h-4 w-4" /> Back
              </button>
              <span className="min-w-0 flex-1 truncate text-center text-xs text-emerald-400">{saveNote}</span>
              <div className="flex gap-2">
                <button onClick={saveFraming} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-200 transition hover:bg-zinc-800"
                        title="Remember this framing for the library and the viewer, without exporting">
                  Save framing
                </button>
                <button onClick={() => startExport()} className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-accent-foreground transition hover:bg-accent-hi">
                  Export {photos.length} photo{photos.length === 1 ? '' : 's'} <ArrowRight className="h-4 w-4" />
                </button>
              </div>
            </div>
          </>
        )}

        {/* ------------------------------------------------ 3. export */}
        {step === 'run' && (
          <>
            <div className="min-h-0 flex-1 overflow-y-auto p-6">
              {runError ? (
                <div className="mx-auto max-w-lg text-center">
                  <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-red-400" />
                  <p className="text-sm text-zinc-200">{runError}</p>
                </div>
              ) : !job ? (
                <div className="flex items-center justify-center gap-3 py-10 text-sm text-zinc-400"><Loader2 className="h-5 w-5 animate-spin" /> Starting…</div>
              ) : job.state === 'running' ? (
                <div className="mx-auto max-w-lg py-6">
                  <div className="mb-2 flex items-baseline justify-between text-sm">
                    <span className="text-zinc-200">{job.current === 'Packing the ZIP' ? 'Packing the ZIP…' : `Exporting ${Math.min(job.done + 1, job.total)} of ${job.total}`}</span>
                    <span className="tabular-nums text-zinc-500">{pct}%</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-zinc-800">
                    <div className="h-full rounded-full bg-accent transition-all duration-300" style={{ width: `${pct}%` }} />
                  </div>
                  <p className="mt-2 truncate text-xs text-zinc-500">{job.current}</p>
                </div>
              ) : job.state === 'error' ? (
                <div className="mx-auto max-w-lg text-center">
                  <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-red-400" />
                  <p className="text-sm text-zinc-200">{job.error || 'The export failed.'}</p>
                  {job.failed?.length > 0 && (
                    <ul className="mt-3 space-y-1 text-left text-xs text-zinc-400">
                      {job.failed.slice(0, 10).map((f) => <li key={f.id}><span className="text-zinc-300">{f.filename}</span> - {f.reason}</li>)}
                    </ul>
                  )}
                </div>
              ) : (
                <div className="mx-auto max-w-xl">
                  <div className="text-center">
                    <div className="mx-auto mb-3 grid h-12 w-12 place-items-center rounded-full bg-emerald-500/15 text-emerald-400"><Check className="h-6 w-6" /></div>
                    <p className="text-base font-semibold text-zinc-100">{job.ok} photo{job.ok === 1 ? '' : 's'} ready</p>
                    <p className="mt-0.5 text-sm text-zinc-500">{job.zip_name} · {job.zip_kb >= 1024 ? `${(job.zip_kb / 1024).toFixed(1)} MB` : `${job.zip_kb} KB`}</p>
                    <a href={`${API_BASE}/api/photo-export/jobs/${job.id}/download?token=${token()}`} download={job.zip_name}
                       className="mt-4 inline-flex items-center gap-2 rounded-xl bg-accent px-6 py-3 text-sm font-semibold text-accent-foreground shadow-lg shadow-accent/20 transition hover:bg-accent-hi active:scale-95">
                      <Download className="h-4 w-4" /> Download ZIP
                    </a>
                    <p className="mt-2 text-[11px] text-zinc-600">The ZIP is kept on the server for 24 hours.</p>
                  </div>
                  {job.failed?.length > 0 && (
                    <div className="mt-5 rounded-xl border border-red-500/30 bg-red-500/10 p-3">
                      <p className="mb-1 text-xs font-medium text-red-300">{job.failed.length} could not be exported</p>
                      <ul className="space-y-0.5 text-xs text-red-200/80">
                        {job.failed.slice(0, 20).map((f) => <li key={f.id}>{f.filename} - {f.reason}</li>)}
                      </ul>
                    </div>
                  )}
                  {job.warnings?.length > 0 && (
                    <div className="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3">
                      <p className="mb-1 text-xs font-medium text-amber-300">Worth a look</p>
                      <ul className="space-y-0.5 text-xs text-amber-200/80">
                        {job.warnings.slice(0, 20).map((w, i) => <li key={i}>{w.filename} - {w.note}</li>)}
                      </ul>
                    </div>
                  )}
                </div>
              )}
            </div>
            <div className="flex shrink-0 items-center justify-between gap-3 border-t border-zinc-800 px-5 py-3.5">
              <button onClick={() => { clearTimeout(pollRef.current); setJob(null); setRunError(''); setStep('frame'); setPhotos((p) => p); }}
                      disabled={job?.state === 'running'}
                      className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-40">
                <ArrowLeft className="h-4 w-4" /> Adjust framing
              </button>
              <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800">
                {job?.state === 'done' ? 'Done' : 'Close'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}
