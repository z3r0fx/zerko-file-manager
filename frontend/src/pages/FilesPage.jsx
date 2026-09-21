import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { createPortal } from 'react-dom';
import { useSidebarSlot } from '../lib/sidebarSlot';
import {
  HardDrive, Clock, Star, Link2, Trash2, Plus, FolderPlus, FilePlus2, FolderUp, LayoutGrid, List as ListIcon,
  Info, ArrowUpDown, ChevronRight, MoreHorizontal, X, Download, Share2, Pencil, FolderInput, Copy, Scissors,
  ClipboardPaste, RefreshCw, FolderOpen, Search, UploadCloud, Eye, AlertTriangle, Folder,
} from 'lucide-react';
import { cn } from '../lib/utils';
import { useAuth } from '../context/AuthContext';
import { CAP } from '../lib/capabilities';
import ContextMenu from '../components/shared/ContextMenu';
import {
  filesApi, uploads, sortEntries, formatBytes, downloadUrl, zipUrl, startDownload, parentOf, joinPath,
  collectDrop, ensureDirs, DND_TYPE,
} from '../lib/files';
import { Button, Dropdown, NameDialog, ConfirmDialog, FolderPicker, Toasts, useToasts } from '../components/files/Dialogs';
import { GridView, ListView } from '../components/files/EntryViews';
import { Empty, Loading, TrashView, LinksView, fullLink } from '../components/files/SideViews';
import DetailsPanel from '../components/files/DetailsPanel';
import PreviewModal from '../components/files/PreviewModal';
import ShareLinkDialog from '../components/files/ShareLinkDialog';
import FolderTree from '../components/files/FolderTree';

const LS = 'zerko.files.';
const lsGet = (k, d) => { try { const v = localStorage.getItem(LS + k); return v == null ? d : v; } catch { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(LS + k, v); } catch { /* private mode */ } };
const RENDER_CAP = 1500;

const PLACES = [
  { id: 'files', label: 'All files', icon: HardDrive },
  { id: 'recent', label: 'Recent', icon: Clock },
  { id: 'starred', label: 'Starred', icon: Star },
  { id: 'shared', label: 'Shared links', icon: Link2 },
  { id: 'trash', label: 'Trash', icon: Trash2 },
];

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { /* nothing */ }
  ta.remove();
  return ok;
}

const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

// ------------------------------------------------------------------ small pieces
function Breadcrumbs({ crumbs, onGo, dropTarget, setDropTarget, onDropItems, canDrop }) {
  const all = [{ name: 'All files', path: '' }, ...crumbs];
  const collapsed = all.length > 4;
  const hidden = collapsed ? all.slice(1, all.length - 2) : [];
  const shown = collapsed ? [all[0], null, ...all.slice(all.length - 2)] : all;
  const Crumb = ({ c, last }) => (
    <button type="button" onClick={() => onGo(c.path)} data-drop-path={c.path}
            onDragOver={canDrop ? (e) => {
              const t = [...e.dataTransfer.types];
              if (!t.includes(DND_TYPE) && !t.includes('Files')) return;
              e.preventDefault(); e.stopPropagation(); setDropTarget(c.path);
            } : undefined}
            onDragLeave={() => setDropTarget((d) => (d === c.path ? null : d))}
            onDrop={canDrop ? (e) => { e.preventDefault(); e.stopPropagation(); setDropTarget(null); onDropItems(e, c.path); } : undefined}
            className={cn('max-w-[16rem] truncate rounded-lg px-2.5 py-1.5 text-sm transition',
              last ? 'font-semibold text-zinc-50' : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100',
              dropTarget === c.path && 'bg-accent/25 text-zinc-50 ring-1 ring-accent')}>
      {c.name}
    </button>
  );
  return (
    <nav aria-label="Folder path" className="flex min-w-0 items-center">
      {shown.map((c, i) => (
        <span key={c ? c.path || 'root' : 'more'} className="flex min-w-0 items-center">
          {i > 0 && <ChevronRight className="h-4 w-4 shrink-0 text-zinc-600" />}
          {c ? <Crumb c={c} last={i === shown.length - 1} /> : (
            <Dropdown trigger={<button type="button" aria-label="Show hidden folders" className="rounded-lg px-2 py-1.5 text-zinc-400 hover:bg-zinc-800"><MoreHorizontal className="h-4 w-4" /></button>}
                      items={hidden.map((h) => ({ label: h.name, icon: Folder, onClick: () => onGo(h.path) }))} />
          )}
        </span>
      ))}
    </nav>
  );
}

function StorageMeter({ usage }) {
  if (!usage) return <div className="h-14 animate-pulse rounded-xl bg-zinc-900/60" />;
  const total = usage.disk_total || 0;
  const pct = total ? Math.min(100, (usage.used / total) * 100) : 0;
  return (
    <div className="rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-3.5">
      <p className="text-[13px] font-medium text-zinc-200">
        {usage.computing ? 'Counting...' : formatBytes(usage.used)} <span className="font-normal text-zinc-500">stored</span>
      </p>
      {total > 0 && (
        <>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-zinc-800">
            <div className="h-full rounded-full bg-accent transition-[width] duration-700" style={{ width: `${Math.max(pct, usage.used ? 1.5 : 0)}%` }} />
          </div>
          <p className="mt-2 text-[11px] text-zinc-500">{formatBytes(usage.disk_free)} free on this drive</p>
        </>
      )}
      <p className="mt-1 text-[11px] text-zinc-600">{plural(usage.files, 'file')} - {plural(usage.folders, 'folder')}</p>
    </div>
  );
}

// ------------------------------------------------------------------ the page
export default function FilesPage() {
  const { can: canDo } = useAuth();
  const can = useMemo(() => ({
    write: canDo(CAP.UPLOAD), share: canDo(CAP.SHARES), download: canDo(CAP.DOWNLOAD), purge: canDo(CAP.HARD_DELETE),
  }), [canDo]);

  const [params, setParams] = useSearchParams();
  const place = PLACES.some((p) => p.id === params.get('v')) ? params.get('v') : 'files';
  const path = place === 'files' ? (params.get('p') || '') : '';

  const [query, setQuery] = useState('');
  const [dq, setDq] = useState('');
  const [scopeAll, setScopeAll] = useState(false);
  const [data, setData] = useState(null);           // { key, entries, crumbs, truncated } | { key, error }
  const [trash, setTrash] = useState(null);
  const [links, setLinks] = useState(null);
  const [usage, setUsage] = useState(null);
  const [version, setVersion] = useState(0);
  const [showAll, setShowAll] = useState(false);

  const [selected, setSelected] = useState(() => new Set());
  const [focus, setFocus] = useState(null);
  const [clip, setClip] = useState(null);           // { paths, mode: 'cut' | 'copy' }
  const [viewMode, setViewMode] = useState(lsGet('view', 'grid'));
  const [sort, setSort] = useState(() => { try { return JSON.parse(lsGet('sort', '')) || { key: 'name', dir: 'asc' }; } catch { return { key: 'name', dir: 'asc' }; } });
  const [details, setDetails] = useState(lsGet('details', '0') === '1');
  const [dialog, setDialog] = useState(null);
  const [preview, setPreview] = useState(null);     // path of the file being previewed
  const [menu, setMenu] = useState(null);
  const [dropTarget, setDropTarget] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [marquee, setMarquee] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const { toasts, push: toast, dismiss } = useToasts();

  const scrollRef = useRef(null);
  const anchor = useRef(null);
  const dragDepth = useRef(0);
  const pendingSelect = useRef(null);
  const fileInput = useRef(null);
  const dirInput = useRef(null);
  const coarse = useRef(typeof window !== 'undefined' && !!window.matchMedia?.('(pointer: coarse)').matches);

  const bump = useCallback(() => setVersion((v) => v + 1), []);

  // ---- URL
  const go = useCallback((next) => {
    const p = new URLSearchParams();
    if (next.v && next.v !== 'files') p.set('v', next.v);
    if (next.p) p.set('p', next.p);
    setParams(p);
    setQuery(''); setDq('');
    window.dispatchEvent(new CustomEvent('zerko-search-clear'));
  }, [setParams]);
  const openFolder = useCallback((p) => go({ p }), [go]);

  // ---- search box lives in the top bar
  useEffect(() => {
    const on = (e) => setQuery(String(e.detail || ''));
    window.addEventListener('odyssey-search', on);
    return () => window.removeEventListener('odyssey-search', on);
  }, []);
  useEffect(() => { const t = setTimeout(() => setDq(query.trim()), 250); return () => clearTimeout(t); }, [query]);

  // ---- data
  const key = `${place}|${path}|${dq}|${dq ? scopeAll : ''}`;
  useEffect(() => {
    const ac = new AbortController();
    let dead = false;
    const fail = (e) => {
      if (dead || e.name === 'AbortError') return;
      if (place === 'trash') { setTrash({ items: [], days: 30, total_size: 0 }); toast(e.message, { tone: 'error' }); }
      else if (place === 'shared') { setLinks([]); toast(e.message, { tone: 'error' }); }
      else setData({ key, error: e.message, status: e.status });
    };
    if (place === 'trash') {
      filesApi.trash().then((r) => { if (!dead) setTrash(r); }).catch(fail);
    } else if (place === 'shared') {
      filesApi.shares().then((r) => { if (!dead) setLinks(r.shares); }).catch(fail);
    } else if (dq) {
      filesApi.search(dq, scopeAll ? '' : path, ac.signal)
        .then((r) => { if (!dead) setData({ key, entries: r.entries, truncated: r.truncated, search: true }); }).catch(fail);
    } else if (place === 'recent') {
      filesApi.recent().then((r) => { if (!dead) setData({ key, entries: r.entries }); }).catch(fail);
    } else if (place === 'starred') {
      filesApi.starred().then((r) => { if (!dead) setData({ key, entries: r.entries }); }).catch(fail);
    } else {
      filesApi.list(path, { signal: ac.signal })
        .then((r) => { if (!dead) setData({ key, entries: r.entries, crumbs: r.breadcrumbs, truncated: r.truncated, name: r.name }); }).catch(fail);
    }
    return () => { dead = true; ac.abort(); };
  }, [key, version]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { filesApi.usage().then(setUsage).catch(() => {}); }, [version]);
  useEffect(() => { setShowAll(false); }, [key]);

  // selection is per-view
  useEffect(() => { setSelected(new Set()); setFocus(null); anchor.current = null; setMenu(null); }, [place, path, dq]);

  const ready = data && data.key === key;
  const entries = useMemo(() => {
    if (!ready || !data.entries) return [];
    if (place === 'recent' && !dq) return data.entries;
    return sortEntries(data.entries, sort.key, sort.dir);
  }, [ready, data, sort, place, dq]);
  const shown = showAll ? entries : entries.slice(0, RENDER_CAP);
  const byPath = useMemo(() => new Map(shown.map((e) => [e.path, e])), [shown]);
  const selEntries = useMemo(() => [...selected].map((p) => byPath.get(p)).filter(Boolean), [selected, byPath]);
  const stats = useMemo(() => ({
    folders: entries.filter((e) => e.is_dir).length,
    files: entries.filter((e) => !e.is_dir).length,
    size: entries.reduce((s, e) => s + (e.is_dir ? 0 : e.size), 0),
  }), [entries]);

  // after "show in folder", pick and reveal the thing
  useEffect(() => {
    if (!ready || !pendingSelect.current) return;
    const p = pendingSelect.current;
    if (byPath.has(p)) {
      pendingSelect.current = null;
      setSelected(new Set([p])); setFocus(p); anchor.current = p;
      requestAnimationFrame(() => scrollTo(p));
    }
  }, [ready, byPath]); // eslint-disable-line react-hooks/exhaustive-deps

  // finished uploads refresh whatever we are looking at
  useEffect(() => {
    let t;
    const off = uploads.onDone(() => { clearTimeout(t); t = setTimeout(bump, 500); });
    return () => { off(); clearTimeout(t); };
  }, [bump]);

  const scrollTo = (p) => {
    const el = [...(scrollRef.current?.querySelectorAll('[data-path]') || [])].find((n) => n.dataset.path === p);
    el?.scrollIntoView({ block: 'nearest' });
  };

  // ---- actions --------------------------------------------------------
  const fail = (e) => toast(e.message || 'Something went wrong', { tone: 'error' });

  const download = useCallback((list) => {
    if (!can.download) { toast('Your account cannot download files.', { tone: 'error' }); return; }
    if (list.length === 1 && !list[0].is_dir) startDownload(downloadUrl(list[0].path), list[0].name);
    else startDownload(zipUrl(list.map((e) => e.path)));
  }, [can.download, toast]);

  const open = useCallback((e) => {
    if (e.is_dir) { openFolder(e.path); return; }
    if (!can.download) { toast('Your account cannot open or download files.', { tone: 'error' }); return; }
    setPreview(e.path);
  }, [openFolder, can.download, toast]);

  const reveal = useCallback((e) => { pendingSelect.current = e.path; go({ p: parentOf(e.path) }); }, [go]);

  const remove = useCallback(async (list) => {
    if (!list.length) return;
    try {
      const r = await filesApi.remove(list.map((e) => e.path));
      const ok = r.results.filter((x) => x.ok);
      const bad = r.results.filter((x) => !x.ok);
      setSelected(new Set());
      bump();
      // deleted the folder you are standing in (or one above it): step out
      const here = L.current?.path || '';
      const gone = ok.map((x) => x.path).find((q) => here === q || here.startsWith(`${q}/`));
      if (gone) go({ p: parentOf(gone) });
      if (ok.length) {
        const ids = ok.map((x) => x.trash_id).filter(Boolean);
        toast(ok.length === 1 ? `"${list.find((e) => e.path === ok[0].path)?.name || 'Item'}" moved to trash` : `${ok.length} items moved to trash`,
          { action: ids.length ? { label: 'Undo', onClick: async () => { try { await filesApi.restore(ids); bump(); } catch (e) { fail(e); } } } : undefined });
      }
      if (bad.length) toast(bad[0].error, { tone: 'error' });
    } catch (e) { fail(e); }
  }, [bump, go]); // eslint-disable-line react-hooks/exhaustive-deps

  const transfer = useCallback(async (paths, dest, mode = 'move') => {
    try {
      const r = await filesApi.move(paths, dest, mode);
      const ok = r.results.filter((x) => x.ok).length;
      const bad = r.results.filter((x) => !x.ok);
      bump();
      if (ok) toast(`${mode === 'copy' ? 'Copied' : 'Moved'} ${plural(ok, 'item')} to ${dest ? dest.split('/').pop() : 'All files'}`, { tone: 'success' });
      if (bad.length) toast(bad[0].error, { tone: 'error' });
      return ok;
    } catch (e) { fail(e); return 0; }
  }, [bump]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleStar = useCallback(async (e) => {
    const next = !e.starred;
    setData((d) => (d && d.entries ? { ...d, entries: d.entries.map((x) => (x.path === e.path ? { ...x, starred: next } : x)) } : d));
    try { await filesApi.star(e.path, next); if (place === 'starred') bump(); } catch (err) { fail(err); bump(); }
  }, [place, bump]); // eslint-disable-line react-hooks/exhaustive-deps

  const startUploads = useCallback(async (drop, dest) => {
    if (!can.write) { toast('Your account cannot upload files.', { tone: 'error' }); return; }
    if (!drop.files.length && !drop.emptyDirs.length) { toast('Nothing to upload.'); return; }
    try {
      if (drop.emptyDirs.length) { await ensureDirs(dest, drop.emptyDirs); bump(); }
      if (drop.files.length) uploads.add(drop.files, dest);
    } catch (e) { fail(e); }
  }, [can.write, bump]); // eslint-disable-line react-hooks/exhaustive-deps

  const dropItems = useCallback(async (e, dest) => {
    const dt = e.dataTransfer;
    const types = [...dt.types];
    if (types.includes(DND_TYPE)) {
      let paths = [];
      try { paths = JSON.parse(dt.getData(DND_TYPE)); } catch { /* ignore */ }
      const mode = e.ctrlKey || e.altKey ? 'copy' : 'move';
      paths = paths.filter((p) => p !== dest && !dest.startsWith(`${p}/`) && (mode === 'copy' || parentOf(p) !== dest));
      if (paths.length) await transfer(paths, dest, mode);
    } else if (types.includes('Files')) {
      const drop = await collectDrop(dt);            // must run before anything else awaits
      await startUploads(drop, dest);
    }
  }, [transfer, startUploads]);

  const paste = useCallback(async () => {
    if (!clip || !can.write) return;
    const ok = await transfer(clip.paths, path, clip.mode === 'cut' ? 'move' : 'copy');
    if (ok && clip.mode === 'cut') setClip(null);
  }, [clip, can.write, path, transfer]);

  const pickFiles = (files) => {
    if (!files.length) return;
    uploads.add(files.map((f) => ({ file: f, relativePath: f.webkitRelativePath || null })), path);
  };

  const onRestore = async (ids) => {
    try { await filesApi.restore(ids); toast('Restored', { tone: 'success' }); bump(); } catch (e) { fail(e); }
  };

  const copyLink = async (s) => { toast((await copyText(fullLink(s))) ? 'Link copied' : 'Could not copy - select it manually', { tone: 'success' }); };
  const revokeLink = async (s) => {
    setBusyId(s.id);
    try { await filesApi.revokeShare(s.id); toast('Link turned off'); bump(); } catch (e) { fail(e); } finally { setBusyId(null); }
  };

  const actions = {
    download, open, remove,
    share: (e) => setDialog({ type: 'share', entry: e }),
    rename: (e) => setDialog({ type: 'rename', entry: e }),
    moveTo: (list) => setDialog({ type: 'move', entries: list, mode: 'move' }),
    star: toggleStar,
  };

  // ---- selection --------------------------------------------------------
  const order = useMemo(() => shown.map((e) => e.path), [shown]);

  const onClick = (ev, entry) => {
    if (ev.detail > 1) return;
    const p = entry.path;
    setFocus(p);
    if (ev.shiftKey && anchor.current && order.includes(anchor.current)) {
      const a = order.indexOf(anchor.current); const b = order.indexOf(p);
      setSelected(new Set(order.slice(Math.min(a, b), Math.max(a, b) + 1)));
      return;
    }
    anchor.current = p;
    if (ev.ctrlKey || ev.metaKey) {
      setSelected((s) => { const n = new Set(s); if (n.has(p)) n.delete(p); else n.add(p); return n; });
    } else if (coarse.current) {
      if (selected.size) setSelected((s) => { const n = new Set(s); if (n.has(p)) n.delete(p); else n.add(p); return n; });
      else open(entry);
    } else setSelected(new Set([p]));
  };
  const onCheck = (entry) => {
    anchor.current = entry.path; setFocus(entry.path);
    setSelected((s) => { const n = new Set(s); if (n.has(entry.path)) n.delete(entry.path); else n.add(entry.path); return n; });
  };

  const onDragStart = (e, entry) => {
    const paths = selected.has(entry.path) ? [...selected] : [entry.path];
    if (!selected.has(entry.path)) setSelected(new Set([entry.path]));
    e.dataTransfer.setData(DND_TYPE, JSON.stringify(paths));
    e.dataTransfer.effectAllowed = 'copyMove';
    if (paths.length === 1 && !entry.is_dir && can.download) {
      e.dataTransfer.setData('DownloadURL', `application/octet-stream:${entry.name}:${window.location.origin}${downloadUrl(entry.path)}`);
    }
    if (paths.length > 1) {
      const g = document.createElement('div');
      g.textContent = `${paths.length} items`;
      g.style.cssText = 'position:fixed;top:-100px;left:-100px;padding:8px 14px;border-radius:10px;background:#3f3f46;color:#fff;font:600 13px system-ui';
      document.body.appendChild(g);
      e.dataTransfer.setDragImage(g, 20, 16);
      setTimeout(() => g.remove(), 0);
    }
  };

  // ---- context menus ------------------------------------------------------
  const menuFor = (e, entry) => {
    if (place === 'trash' || place === 'shared') return;
    let list = selEntries;
    if (!selected.has(entry.path)) { list = [entry]; setSelected(new Set([entry.path])); setFocus(entry.path); anchor.current = entry.path; }
    const one = list.length === 1 ? list[0] : null;
    const items = [];
    if (one) items.push({ label: one.is_dir ? 'Open' : 'Preview', icon: one.is_dir ? FolderOpen : Eye, onClick: () => open(one) });
    if (can.download) items.push({ label: one && !one.is_dir ? 'Download' : 'Download as zip', icon: Download, onClick: () => download(list) });
    if (one && can.share) items.push({ label: 'Share', icon: Share2, onClick: () => actions.share(one) });
    if (one) items.push({ label: one.starred ? 'Remove star' : 'Add star', icon: Star, onClick: () => toggleStar(one) });
    if (can.write) {
      items.push({ type: 'divider' });
      if (one) items.push({ label: 'Rename', icon: Pencil, onClick: () => actions.rename(one) });
      items.push({ label: 'Move to...', icon: FolderInput, onClick: () => actions.moveTo(list) });
      items.push({ label: 'Make a copy', icon: Copy, onClick: () => transfer(list.map((x) => x.path), parentOf(list[0].path), 'copy') });
      items.push({ label: 'Cut', icon: Scissors, onClick: () => { setClip({ paths: list.map((x) => x.path), mode: 'cut' }); toast(`${plural(list.length, 'item')} ready to move - open a folder and paste`); } });
    }
    if (one && (place !== 'files' || dq)) items.push({ label: 'Show in folder', icon: FolderOpen, onClick: () => reveal(one) });
    if (can.write) { items.push({ type: 'divider' }); items.push({ label: 'Move to trash', icon: Trash2, danger: true, onClick: () => remove(list) }); }
    setMenu({ x: e.clientX, y: e.clientY, items });
  };

  // Right-click a folder in the sidebar tree. Leaves the main selection alone.
  const treeMenu = (e, node) => {
    const entry = { ...node, is_dir: true, kind: 'folder' };
    const items = [{ label: 'Open', icon: FolderOpen, onClick: () => open(entry) }];
    if (can.download) items.push({ label: 'Download as zip', icon: Download, onClick: () => download([entry]) });
    if (can.share) items.push({ label: 'Share', icon: Share2, onClick: () => actions.share(entry) });
    if (can.write) {
      items.push({ type: 'divider' });
      items.push({ label: 'Rename', icon: Pencil, onClick: () => actions.rename(entry) });
      items.push({ label: 'Move to...', icon: FolderInput, onClick: () => actions.moveTo([entry]) });
      items.push({ type: 'divider' });
      items.push({ label: 'Delete', icon: Trash2, danger: true, onClick: () => remove([entry]) });
    }
    setMenu({ x: e.clientX, y: e.clientY, items });
  };

  const emptyMenu = (e) => {
    if (e.target.closest('[data-path]')) return;
    e.preventDefault();
    if (place !== 'files' || dq) return;
    const items = [];
    if (can.write) {
      items.push({ label: 'New folder', icon: FolderPlus, onClick: () => setDialog({ type: 'mkdir' }) });
      items.push({ label: 'Upload files', icon: FilePlus2, onClick: () => fileInput.current?.click() });
      items.push({ label: 'Upload folder', icon: FolderUp, onClick: () => dirInput.current?.click() });
      if (clip) items.push({ label: 'Paste here', icon: ClipboardPaste, onClick: paste });
      items.push({ type: 'divider' });
    }
    items.push({ label: 'Refresh', icon: RefreshCw, onClick: bump });
    setMenu({ x: e.clientX, y: e.clientY, items });
  };

  // ---- marquee ---------------------------------------------------------------
  const onPointerDown = (e) => {
    if (e.button !== 0 || e.pointerType === 'touch' || e.target.closest('[data-path], button, a, input, select')) return;
    const box = scrollRef.current;
    if (!box) return;
    const r0 = box.getBoundingClientRect();
    const toContent = (x, y) => ({ x: x - r0.left + box.scrollLeft, y: y - r0.top + box.scrollTop });
    const start = toContent(e.clientX, e.clientY);
    const base = e.ctrlKey || e.metaKey || e.shiftKey ? new Set(selected) : new Set();
    if (!base.size) setSelected(new Set());
    let moved = false;
    const move = (ev) => {
      const cur = toContent(ev.clientX, ev.clientY);
      if (!moved && Math.hypot(cur.x - start.x, cur.y - start.y) < 5) return;
      moved = true;
      const rect = { x: Math.min(start.x, cur.x), y: Math.min(start.y, cur.y), w: Math.abs(cur.x - start.x), h: Math.abs(cur.y - start.y) };
      const hit = new Set(base);
      box.querySelectorAll('[data-path]').forEach((el) => {
        const b = el.getBoundingClientRect();
        const c = toContent(b.left, b.top);
        if (c.x < rect.x + rect.w && c.x + b.width > rect.x && c.y < rect.y + rect.h && c.y + b.height > rect.y) hit.add(el.dataset.path);
      });
      setSelected(hit);
      setMarquee(rect);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      setMarquee(null);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };

  // ---- keyboard -----------------------------------------------------------------
  const L = useRef({});
  L.current = { shown, order, selEntries, focus, selected, place, path, preview, dialog, menu, clip, can, dq };
  useEffect(() => {
    const step = (dir) => {
      const { order: ord, focus: f } = L.current;
      if (!ord.length) return null;
      const i = f ? ord.indexOf(f) : -1;
      if (dir === 'left' || dir === 'right') return ord[Math.max(0, Math.min(ord.length - 1, i + (dir === 'right' ? 1 : -1)))] ?? ord[0];
      const els = [...scrollRef.current.querySelectorAll('[data-path]')];
      const cur = els.find((n) => n.dataset.path === f);
      if (!cur) return ord[0];
      const cb = cur.getBoundingClientRect();
      const cx = cb.left + cb.width / 2; const cy = cb.top + cb.height / 2;
      let best = null; let bestScore = Infinity;
      for (const el of els) {
        if (el === cur) continue;
        const b = el.getBoundingClientRect();
        const dy = (b.top + b.height / 2) - cy;
        if (dir === 'down' ? dy <= 4 : dy >= -4) continue;
        const dx = Math.abs((b.left + b.width / 2) - cx);
        const score = Math.abs(dy) * 4 + dx;
        if (score < bestScore) { bestScore = score; best = el; }
      }
      return best ? best.dataset.path : f;
    };

    const onKey = (ev) => {
      const s = L.current;
      if (s.preview || s.dialog || s.menu) return;
      if (ev.target.closest?.('input, textarea, select, [contenteditable="true"]')) return;
      if (s.place === 'trash' || s.place === 'shared') return;
      const mod = ev.ctrlKey || ev.metaKey;
      const k = ev.key;
      const pick = (p, extend) => {
        if (!p) return;
        setFocus(p);
        if (extend && s.order.includes(anchor.current || p)) {
          const a = s.order.indexOf(anchor.current || p); const b = s.order.indexOf(p);
          setSelected(new Set(s.order.slice(Math.min(a, b), Math.max(a, b) + 1)));
        } else { anchor.current = p; setSelected(new Set([p])); }
        scrollTo(p);
      };
      if (k === 'ArrowRight' || k === 'ArrowLeft' || k === 'ArrowDown' || k === 'ArrowUp') {
        ev.preventDefault();
        pick(step(k.slice(5).toLowerCase()), ev.shiftKey);
      } else if (mod && k.toLowerCase() === 'a') { ev.preventDefault(); setSelected(new Set(s.order)); }
      else if (k === 'Escape') { setSelected(new Set()); }
      else if (k === 'Enter' && s.selEntries.length === 1) { ev.preventDefault(); open(s.selEntries[0]); }
      else if (k === ' ' && s.selEntries.length === 1) { ev.preventDefault(); if (!s.selEntries[0].is_dir) open(s.selEntries[0]); }
      else if (k === 'F2' && s.selEntries.length === 1 && s.can.write) { ev.preventDefault(); actions.rename(s.selEntries[0]); }
      else if ((k === 'Delete' || (k === 'Backspace' && mod)) && s.selEntries.length && s.can.write) { ev.preventDefault(); remove(s.selEntries); }
      else if (k === 'Backspace' && !s.selEntries.length && s.path && !s.dq) { ev.preventDefault(); openFolder(parentOf(s.path)); }
      else if (mod && (k.toLowerCase() === 'x' || k.toLowerCase() === 'c') && s.selEntries.length && s.can.write) {
        ev.preventDefault();
        setClip({ paths: s.selEntries.map((e) => e.path), mode: k.toLowerCase() === 'x' ? 'cut' : 'copy' });
        toast(`${plural(s.selEntries.length, 'item')} ${k.toLowerCase() === 'x' ? 'cut' : 'copied'} - open a folder and press Ctrl+V`);
      } else if (mod && k.toLowerCase() === 'v' && s.clip) { ev.preventDefault(); paste(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }); // re-bound each render on purpose: handlers close over fresh actions

  // ---- page-level drag & drop (files from the computer) -----------------------
  const uploadable = can.write && (place === 'files');
  const onDragEnter = (e) => { if (uploadable && [...e.dataTransfer.types].includes('Files')) { dragDepth.current += 1; setDragOver(true); } };
  const onDragLeave = (e) => { if ([...e.dataTransfer.types].includes('Files')) { dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDragOver(false); } };
  const onDragOver = (e) => {
    const t = [...e.dataTransfer.types];
    if (uploadable && t.includes('Files')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }
  };
  const onDrop = (e) => {
    dragDepth.current = 0; setDragOver(false);
    if (uploadable && [...e.dataTransfer.types].includes('Files')) { e.preventDefault(); dropItems(e, path); }
  };

  // ---- render -----------------------------------------------------------------
  const h = {
    selected, focus, cut: new Set(clip?.mode === 'cut' ? clip.paths : []), dropTarget, setDropTarget,
    canDrag: can.write && place !== 'trash', canDrop: can.write, showLocation: place !== 'files' || !!dq,
    onClick, onCheck, onOpen: open, onContext: menuFor, onDragStart, onDropItems: dropItems,
  };

  const setSortKey = (k) => setSort((s) => { const n = s.key === k ? { key: k, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: k === 'modified' || k === 'size' ? 'desc' : 'asc' }; lsSet('sort', JSON.stringify(n)); return n; });
  const setView = (v) => { setViewMode(v); lsSet('view', v); };
  const toggleDetails = () => setDetails((d) => { lsSet('details', d ? '0' : '1'); return !d; });

  const folderTitle = dq ? `Results for "${dq}"`
    : place === 'files' ? (path ? path.split('/').pop() : 'All files')
    : PLACES.find((p) => p.id === place).label;
  const previewFiles = shown.filter((e) => !e.is_dir);
  const previewIndex = preview ? previewFiles.findIndex((e) => e.path === preview) : -1;

  const sortItems = [
    ...[['name', 'Name'], ['modified', 'Date modified'], ['size', 'Size'], ['kind', 'Type']].map(([k, label]) => ({
      label, active: sort.key === k, onClick: () => { if (sort.key !== k) setSortKey(k); },
    })),
    { type: 'divider' },
    { label: 'Ascending', active: sort.dir === 'asc', onClick: () => { setSort((s) => { const n = { ...s, dir: 'asc' }; lsSet('sort', JSON.stringify(n)); return n; }); } },
    { label: 'Descending', active: sort.dir === 'desc', onClick: () => { setSort((s) => { const n = { ...s, dir: 'desc' }; lsSet('sort', JSON.stringify(n)); return n; }); } },
  ];

  const newItems = [
    { label: 'New folder', icon: FolderPlus, onClick: () => setDialog({ type: 'mkdir' }) },
    { type: 'divider' },
    { label: 'Upload files', icon: FilePlus2, onClick: () => fileInput.current?.click() },
    { label: 'Upload folder', icon: FolderUp, onClick: () => dirInput.current?.click() },
  ];
  const newButton = (className) => (
    <Dropdown className={className} items={newItems}
              trigger={<button type="button" className="flex w-full items-center justify-center gap-2 rounded-2xl bg-accent px-4 py-2.5 text-sm font-semibold text-accent-foreground shadow-lg shadow-black/30 transition hover:bg-accent-hi">
                <Plus className="h-[18px] w-[18px]" strokeWidth={2.5} /> New
              </button>} />
  );

  const body = () => {
    if (place === 'trash') return <TrashView data={trash} canPurge={can.purge}
                                             onRestore={onRestore}
                                             onPurge={(t) => setDialog({ type: 'purge', item: t })}
                                             onEmpty={() => setDialog({ type: 'empty' })} />;
    if (place === 'shared') return <LinksView data={links} busyId={busyId} onCopy={copyLink} onRevoke={revokeLink}
                                              onReveal={(s) => reveal({ path: s.path })} />;
    if (!ready) return <Loading />;
    if (data.error) {
      return (
        <Empty icon={AlertTriangle} title={data.status === 404 || data.status === 400 ? 'That folder is not here any more' : 'Could not load this'}
               hint={data.error}>
          <Button variant="outline" onClick={() => go({})}>Go to All files</Button>
          <Button variant="ghost" onClick={bump}>Try again</Button>
        </Empty>
      );
    }
    if (!entries.length) {
      if (dq) return <Empty icon={Search} title={`Nothing matches "${dq}"`} hint={scopeAll || !path ? 'Try a different word, or part of the name.' : 'Try searching everywhere instead.'}>
        {!scopeAll && path && <Button variant="outline" onClick={() => setScopeAll(true)}>Search everywhere</Button>}
      </Empty>;
      if (place === 'recent') return <Empty icon={Clock} title="Nothing uploaded yet" hint="Files people add show up here, newest first." />;
      if (place === 'starred') return <Empty icon={Star} title="No starred items" hint="Star the things you use most and they will be waiting here." />;
      return (
        <Empty icon={UploadCloud} title={path ? 'This folder is empty' : 'Your storage is empty'}
               hint={can.write ? 'Drag files or whole folders anywhere on this page, or use the buttons below.' : 'Nothing has been added yet.'}>
          {can.write && <Button variant="solid" onClick={() => fileInput.current?.click()}><FilePlus2 className="h-4 w-4" /> Upload files</Button>}
          {can.write && <Button variant="outline" onClick={() => setDialog({ type: 'mkdir' })}><FolderPlus className="h-4 w-4" /> New folder</Button>}
        </Empty>
      );
    }
    return (
      <>
        {data.truncated && (
          <p className="mb-4 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3.5 py-2 text-xs text-amber-200">
            <AlertTriangle className="h-3.5 w-3.5" /> Only the first results are shown. Narrow the search to see the rest.
          </p>
        )}
        {viewMode === 'grid' ? <GridView entries={shown} h={h} /> : <ListView entries={shown} h={h} sort={sort} onSort={setSortKey} />}
        {entries.length > shown.length && (
          <div className="py-6 text-center">
            <Button variant="outline" onClick={() => setShowAll(true)}>Show all {entries.length.toLocaleString()} items</Button>
          </div>
        )}
      </>
    );
  };

  const inSelectMode = selEntries.length > 0 && place !== 'trash' && place !== 'shared';
  const one = selEntries.length === 1 ? selEntries[0] : null;
  const selBtn = (icon, label, onClick, danger) => {
    const Icon = icon;
    return (
      <button key={label} onClick={onClick} title={label} aria-label={label}
              className={cn('inline-flex items-center gap-2 rounded-lg px-2.5 py-2 text-sm transition', danger ? 'text-red-400 hover:bg-red-500/10' : 'text-zinc-200 hover:bg-zinc-800')}>
        <Icon className="h-4 w-4" /> <span className="hidden whitespace-nowrap min-[1750px]:inline">{label}</span>
      </button>
    );
  };

  const slot = useSidebarSlot();
  const sidebarPanel = (
    <div className="flex min-h-0 flex-1 flex-col border-t border-zinc-800/80 pt-3">
        <div className="px-4 pb-2">
          {can.write ? newButton() : <div className="px-1 text-xs uppercase tracking-wider text-zinc-600">Shared storage</div>}
        </div>
        <div className="px-3 pt-2">
          {PLACES.map((p) => {
            const active = place === p.id && (p.id !== 'files' || !path);
            return (
              <button key={p.id} type="button" onClick={() => go({ v: p.id })}
                      data-drop-path={p.id === 'files' ? '' : undefined}
                      onDragOver={p.id === 'files' && can.write ? (e) => { const t = [...e.dataTransfer.types]; if (t.includes(DND_TYPE) || t.includes('Files')) { e.preventDefault(); setDropTarget(''); } } : undefined}
                      onDragLeave={() => setDropTarget((d) => (d === '' ? null : d))}
                      onDrop={p.id === 'files' && can.write ? (e) => { e.preventDefault(); e.stopPropagation(); setDropTarget(null); dropItems(e, ''); } : undefined}
                      className={cn('flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm transition',
                        active ? 'bg-accent/15 font-medium text-accent-hi' : 'text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-100',
                        p.id === 'files' && dropTarget === '' && 'bg-accent/25 ring-1 ring-accent')}>
                <p.icon className="h-[18px] w-[18px]" strokeWidth={1.85} /> {p.label}
              </button>
            );
          })}
        </div>
        <div className="mt-4 flex min-h-0 flex-1 flex-col">
          <p className="px-5 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-600">Folders</p>
          <div className="min-h-0 flex-1 overflow-y-auto pb-2">
            <FolderTree current={place === 'files' ? path : null} version={version} onOpen={openFolder} onDropItems={dropItems} onContext={treeMenu} />
          </div>
        </div>
        <div className="p-3 pt-2"><StorageMeter usage={usage} /></div>
      </div>
  );

  return (
    <div className="flex h-full min-h-0 bg-zinc-950" onDragEnter={onDragEnter} onDragLeave={onDragLeave} onDragOver={onDragOver} onDrop={onDrop}>
      {slot && createPortal(sidebarPanel, slot)}

      {/* ------------------------------------------------------------- main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex min-h-[3.5rem] shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-zinc-800/80 px-3 py-1.5 sm:px-5">
          {inSelectMode ? (
            <>
              <button onClick={() => setSelected(new Set())} aria-label="Clear selection" className="rounded-lg p-2 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"><X className="h-[18px] w-[18px]" /></button>
              <span className="mr-2 whitespace-nowrap text-sm font-medium text-zinc-100">{selEntries.length} selected</span>
              <div className="flex min-w-0 items-center gap-0.5 overflow-x-auto">
                {can.download && selBtn(Download, one && !one.is_dir ? 'Download' : 'Download zip', () => download(selEntries))}
                {one && can.share && selBtn(Share2, 'Share', () => actions.share(one))}
                {can.write && selBtn(FolderInput, 'Move to', () => actions.moveTo(selEntries))}
                {one && can.write && selBtn(Pencil, 'Rename', () => actions.rename(one))}
                {selBtn(Star, one?.starred ? 'Unstar' : 'Star', () => selEntries.forEach(toggleStar))}
                {can.write && selBtn(Trash2, 'Delete', () => remove(selEntries), true)}
              </div>
            </>
          ) : (
            <>
              <div className="min-w-0 basis-full sm:flex-1 sm:basis-0">
                {place === 'files' && !dq
                  ? <Breadcrumbs crumbs={data && data.key === key && data.crumbs ? data.crumbs : path.split('/').filter(Boolean).map((n, i, a) => ({ name: n, path: a.slice(0, i + 1).join('/') }))}
                                 onGo={openFolder} dropTarget={dropTarget} setDropTarget={setDropTarget} onDropItems={dropItems} canDrop={can.write} />
                  : (
                    <div className="flex items-center gap-2 px-1">
                      <h1 className="truncate text-base font-semibold text-zinc-100">{folderTitle}</h1>
                      {dq && path && (
                        <button onClick={() => setScopeAll((s) => !s)}
                                className="rounded-full border border-zinc-700 px-2.5 py-0.5 text-xs text-zinc-400 hover:bg-zinc-800">
                          {scopeAll ? 'Everywhere' : `In ${path.split('/').pop()}`} - change
                        </button>
                      )}
                    </div>
                  )}
              </div>
              {can.write && place === 'files' && newButton('md:hidden')}
            </>
          )}
          <div className="ml-auto flex shrink-0 items-center gap-0.5">
            {(place === 'files' || place === 'starred' || dq) && (
              <Dropdown align="right" items={sortItems}
                        trigger={<button type="button" title="Sort" aria-label="Sort" className="rounded-lg p-2 text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100"><ArrowUpDown className="h-[18px] w-[18px]" /></button>} />
            )}
            {(place === 'files' || place === 'starred' || place === 'recent') && (
              <div className="ml-1 flex rounded-lg border border-zinc-800 p-0.5">
                {[['grid', LayoutGrid, 'Grid'], ['list', ListIcon, 'List']].map(([v, Ic, label]) => (
                  <button key={v} onClick={() => setView(v)} title={`${label} view`} aria-label={`${label} view`} aria-pressed={viewMode === v}
                          className={cn('rounded-md p-1.5 transition', viewMode === v ? 'bg-zinc-700 text-zinc-50' : 'text-zinc-500 hover:text-zinc-200')}>
                    <Ic className="h-4 w-4" />
                  </button>
                ))}
              </div>
            )}
            {place !== 'trash' && place !== 'shared' && <button onClick={toggleDetails} title="Details" aria-label="Details" aria-pressed={details}
                    className={cn('ml-1 hidden rounded-lg p-2 transition md:block', details ? 'bg-zinc-800 text-zinc-50' : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100')}>
              <Info className="h-[18px] w-[18px]" />
            </button>}
          </div>
        </div>

        {/* places for narrow screens */}
        <div className="flex shrink-0 gap-1.5 overflow-x-auto border-b border-zinc-800/60 px-3 py-2 md:hidden">
          {PLACES.map((p) => (
            <button key={p.id} onClick={() => go({ v: p.id })}
                    className={cn('inline-flex shrink-0 items-center gap-2 rounded-full border px-3 py-1.5 text-xs transition',
                      place === p.id ? 'border-accent/50 bg-accent/15 text-accent-hi' : 'border-zinc-800 text-zinc-400')}>
              <p.icon className="h-3.5 w-3.5" /> {p.label}
            </button>
          ))}
        </div>

        <div className="relative min-h-0 flex-1">
          <div ref={scrollRef} className="absolute inset-0 select-none overflow-y-auto px-3 pb-24 pt-4 sm:px-5"
               onPointerDown={onPointerDown} onContextMenu={emptyMenu}>
            {body()}
            {marquee && (
              <div className="pointer-events-none absolute z-20 rounded-sm border border-accent bg-accent/15"
                   style={{ left: marquee.x, top: marquee.y, width: marquee.w, height: marquee.h }} />
            )}
          </div>
          {dragOver && (
            <div className="pointer-events-none absolute inset-2 z-30 grid place-items-center rounded-3xl border-2 border-dashed border-accent bg-accent/10 backdrop-blur-[1px]">
              <div className="rounded-2xl bg-zinc-900/95 px-8 py-6 text-center shadow-2xl">
                <UploadCloud className="mx-auto h-9 w-9 text-accent" />
                <p className="mt-3 text-base font-semibold text-zinc-50">Drop to upload</p>
                <p className="mt-1 text-sm text-zinc-400">into {path ? path.split('/').pop() : 'All files'}</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {details && place !== 'trash' && place !== 'shared' && (
        <div className="hidden w-80 shrink-0 md:block">
          <DetailsPanel selection={selEntries} folder={{ title: folderTitle }} view={stats} onClose={toggleDetails} actions={actions} can={can} />
        </div>
      )}

      {/* --------------------------------------------------- overlays */}
      <input ref={fileInput} type="file" multiple hidden onChange={(e) => { pickFiles(Array.from(e.target.files || [])); e.target.value = ''; }} />
      <input ref={dirInput} type="file" hidden webkitdirectory="" directory="" multiple
             onChange={(e) => { pickFiles(Array.from(e.target.files || [])); e.target.value = ''; }} />

      {menu && <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />}
      <Toasts toasts={toasts} dismiss={dismiss} />

      {preview && previewIndex >= 0 && (
        <PreviewModal items={previewFiles} index={previewIndex} onIndex={(i) => setPreview(previewFiles[i].path)}
                      onClose={() => setPreview(null)} canShare={can.share} onShare={(e) => { setPreview(null); actions.share(e); }} />
      )}

      <NameDialog open={dialog?.type === 'mkdir'} title="New folder" label="Folder name" initial="New folder" confirmLabel="Create" selectStem={false}
                  onClose={() => setDialog(null)}
                  onSubmit={async (name) => { await filesApi.mkdir(path, name); setDialog(null); bump(); toast(`Created "${name}"`, { tone: 'success' }); }} />
      <NameDialog open={dialog?.type === 'rename'} title="Rename" label="Name" initial={dialog?.entry?.name || ''} selectStem={!dialog?.entry?.is_dir} confirmLabel="Rename"
                  onClose={() => setDialog(null)}
                  onSubmit={async (name) => {
                    if (name !== dialog.entry.name) {
                      const old = dialog.entry.path;
                      await filesApi.rename(old, name);
                      const to = joinPath(parentOf(old), name);
                      if (path === old || path.startsWith(`${old}/`)) go({ p: to + path.slice(old.length) });
                      bump();
                    }
                    setDialog(null);
                  }} />
      <FolderPicker open={dialog?.type === 'move'} title={dialog?.mode === 'copy' ? 'Copy to' : `Move ${plural(dialog?.entries?.length || 0, 'item')} to`}
                    confirmLabel="Move here" startAt={path}
                    excluded={(dialog?.entries || []).filter((e) => e.is_dir).map((e) => e.path)}
                    onClose={() => setDialog(null)}
                    onPick={async (dest) => { await transfer(dialog.entries.map((e) => e.path), dest, dialog.mode || 'move'); setDialog(null); }} />
      {dialog?.type === 'share' && (
        <ShareLinkDialog entry={dialog.entry} toast={toast} onClose={() => setDialog(null)}
                         onChanged={() => { bump(); }} />
      )}
      <ConfirmDialog open={dialog?.type === 'purge'} danger title="Delete forever?" confirmLabel="Delete forever"
                     message={<>"{dialog?.item?.name}" will be permanently deleted. This cannot be undone.</>}
                     onClose={() => setDialog(null)}
                     onConfirm={async () => { await filesApi.purge([dialog.item.id]); setDialog(null); bump(); toast('Deleted for good'); }} />
      <ConfirmDialog open={dialog?.type === 'empty'} danger title="Empty the trash?" confirmLabel="Empty trash"
                     message="Everything in the trash will be permanently deleted. This cannot be undone."
                     onClose={() => setDialog(null)}
                     onConfirm={async () => { await filesApi.emptyTrash(); setDialog(null); bump(); toast('Trash emptied'); }} />
    </div>
  );
}
