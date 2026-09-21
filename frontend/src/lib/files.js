// Client side of the Files feature: API calls, URL builders, formatting, and
// the upload engine. Kept out of the components so the engine keeps running
// (and keeps its queue) while you navigate between folders.

// ------------------------------------------------------------------ API
export const tokenOf = () => {
  try {
    const t = localStorage.getItem('token');
    return t && t !== 'null' && t !== 'undefined' ? t : null;
  } catch { return null; }
};

export const DND_TYPE = 'application/x-zerko-files';

const enc = encodeURIComponent;
const qs = (o) =>
  Object.entries(o)
    .filter(([, v]) => v !== undefined && v !== null)
    .map(([k, v]) => `${enc(k)}=${enc(v)}`)
    .join('&');

async function failure(res) {
  let msg = res.statusText || `Error ${res.status}`;
  try {
    const j = await res.json();
    if (typeof j.detail === 'string') msg = j.detail;
    else if (Array.isArray(j.detail)) msg = j.detail.map((d) => d.msg).join('; ');
  } catch { /* not JSON */ }
  const err = new Error(msg);
  err.status = res.status;
  return err;
}

export async function req(path, { method = 'GET', json, params, signal, headers: extra } = {}) {
  const url = params ? `${path}?${qs(params)}` : path;
  const headers = { ...(extra || {}) };
  const t = tokenOf();
  if (t) headers.Authorization = `Bearer ${t}`;
  let body;
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  }
  const res = await fetch(url, { method, headers, body, signal });
  if (res.status === 401 && !path.startsWith('/api/public/')) {
    const err = new Error('Your session has ended - please sign in again.');
    err.status = 401;
    throw err;
  }
  if (!res.ok) throw await failure(res);
  return res.json();
}

export const filesApi = {
  list: (path, opts) => req('/api/files/list', { params: { path, show_hidden: opts?.hidden ? 'true' : undefined }, signal: opts?.signal }),
  tree: (path) => req('/api/files/tree', { params: { path } }),
  search: (q, path, signal) => req('/api/files/search', { params: { q, path }, signal }),
  recent: () => req('/api/files/recent'),
  starred: () => req('/api/files/starred'),
  info: (path) => req('/api/files/info', { params: { path } }),
  usage: () => req('/api/files/usage'),
  mkdir: (dir, name) => req('/api/files/mkdir', { method: 'POST', json: { dir, name } }),
  rename: (path, name) => req('/api/files/rename', { method: 'POST', json: { path, name } }),
  move: (paths, dest, mode = 'move') => req('/api/files/move', { method: 'POST', json: { paths, dest, mode } }),
  remove: (paths) => req('/api/files/delete', { method: 'POST', json: { paths } }),
  star: (path, starred) => req('/api/files/star', { method: 'POST', json: { path, starred } }),
  text: (path) => req('/api/files/text', { params: { path, token: tokenOf() } }),
  trash: () => req('/api/files/trash'),
  restore: (ids) => req('/api/files/trash/restore', { method: 'POST', json: { ids } }),
  purge: (ids) => req('/api/files/trash/purge', { method: 'POST', json: { ids } }),
  emptyTrash: () => req('/api/files/trash/empty', { method: 'POST' }),
  shares: (path) => req('/api/files/shares', { params: { path } }),
  createShare: (path, password, expires_days) =>
    req('/api/files/shares', { method: 'POST', json: { path, password: password || null, expires_days: expires_days || null } }),
  revokeShare: (id) => req(`/api/files/shares/${id}`, { method: 'DELETE' }),
};

// URLs a browser opens directly (they cannot carry a header, so the token rides along).
export const downloadUrl = (path, inline = false) =>
  `/api/files/download?${qs({ path, inline: inline ? 'true' : undefined, token: tokenOf() })}`;
export const thumbUrl = (path, w = 360, v) =>
  `/api/files/thumb?${qs({ path, w, v, token: tokenOf() })}`;
export const zipUrl = (paths) =>
  `/api/files/zip?${paths.map((p) => `path=${enc(p)}`).join('&')}&token=${enc(tokenOf() || '')}`;

export function startDownload(url, name) {
  const a = document.createElement('a');
  a.href = url;
  if (name) a.download = name;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 1000);
}

// ------------------------------------------------------------------ formatting
export function formatBytes(n, digits = 1) {
  if (n == null || Number.isNaN(n)) return '';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(digits)} ${units[i]}`;
}

export function formatRate(bps) {
  return bps > 0 ? `${formatBytes(bps)}/s` : '';
}

export function formatEta(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  if (seconds < 60) return `${Math.ceil(seconds)}s left`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} min left`;
  const h = Math.floor(seconds / 3600);
  const m = Math.ceil((seconds % 3600) / 60);
  return `${h} h ${m} min left`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const two = (n) => String(n).padStart(2, '0');

export function formatWhen(ts, { long = false } = {}) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const time = `${two(d.getHours())}:${two(d.getMinutes())}`;
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (days === 0) return `Today, ${time}`;
  if (days === 1) return `Yesterday, ${time}`;
  const base = `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== now.getFullYear() ? ` ${d.getFullYear()}` : ''}`;
  return long ? `${base}, ${time}` : base;
}

export const KIND_LABEL = {
  folder: 'Folder', image: 'Image', video: 'Video', audio: 'Audio', pdf: 'PDF', document: 'Document',
  sheet: 'Spreadsheet', code: 'Code', text: 'Text', archive: 'Archive', disk: 'Disk image',
  app: 'Application', font: 'Font', project: 'Project', other: 'File',
};

export const kindLabel = (e) => {
  if (e.kind === 'folder') return 'Folder';
  const label = KIND_LABEL[e.kind] || 'File';
  if (!e.ext) return label;
  if (label.toLowerCase() === e.ext) return label;          // "PDF", not "PDF PDF"
  return `${e.ext.toUpperCase()} ${label === 'File' ? 'file' : label}`;
};

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

// folders always first, then the chosen key
export function sortEntries(list, key, dir) {
  const m = dir === 'asc' ? 1 : -1;
  const by = {
    name: (a, b) => collator.compare(a.name, b.name),
    modified: (a, b) => a.mtime - b.mtime,
    size: (a, b) => a.size - b.size,
    kind: (a, b) => collator.compare(a.kind + a.ext, b.kind + b.ext),
  }[key] || ((a, b) => collator.compare(a.name, b.name));
  return [...list].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    const r = by(a, b) * m;
    return r !== 0 ? r : collator.compare(a.name, b.name);
  });
}

export const parentOf = (p) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');
export const baseName = (p) => (p.includes('/') ? p.slice(p.lastIndexOf('/') + 1) : p);
export const joinPath = (a, b) => (a ? `${a}/${b}` : b);

// Which files the browser can show itself, and which need the server to make a JPEG.
const NATIVE_IMG = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'avif', 'ico']);
export const isNativeImage = (e) => NATIVE_IMG.has(e.ext);
const NATIVE_VIDEO = new Set(['mp4', 'webm', 'mov', 'm4v', 'ogv']);
export const canPlayVideo = (e) => NATIVE_VIDEO.has(e.ext);
const NATIVE_AUDIO = new Set(['mp3', 'wav', 'ogg', 'opus', 'm4a', 'aac', 'flac']);
export const canPlayAudio = (e) => NATIVE_AUDIO.has(e.ext);
export const isPreviewable = (e) =>
  !e.is_dir && (e.kind === 'image' || e.kind === 'pdf' || e.kind === 'text' || e.kind === 'code'
    || e.kind === 'video' || e.kind === 'audio');

// ------------------------------------------------------------------ uploads
const CONCURRENCY = 3;
const MAX_RETRIES = 6;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const EMPTY_SNAPSHOT = [];

class UploadEngine {
  constructor() {
    this.items = [];
    this.listeners = new Set();
    this.running = 0;
    this.seq = 0;
    this.doneHooks = new Set();
    this._emit = this._emit.bind(this);
    if (typeof window !== 'undefined') {
      window.addEventListener('beforeunload', (e) => {
        if (this.items.some((i) => i.status === 'uploading' || i.status === 'queued')) {
          e.preventDefault();
          e.returnValue = '';
        }
      });
    }
  }

  subscribe(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  onDone(fn) { this.doneHooks.add(fn); return () => this.doneHooks.delete(fn); }

  _emit() {
    this.snapshot = this.items.map((i) => ({ ...i }));
    this.listeners.forEach((fn) => fn(this.snapshot));
  }

  getSnapshot() { return this.snapshot || EMPTY_SNAPSHOT; }

  /** entries: [{file, relativePath?}]  dir: destination folder path */
  add(entries, dir) {
    for (const ent of entries) {
      const file = ent.file || ent;
      const rel = ent.relativePath || file.webkitRelativePath || null;
      const dup = this.items.find((i) => i.name === file.name && i.size === file.size && i.dir === dir
        && i.file.lastModified === file.lastModified && ['queued', 'uploading', 'paused'].includes(i.status));
      if (dup) continue;
      this.items.push({
        id: ++this.seq, file, name: file.name, size: file.size, dir,
        relative: rel && rel.includes('/') ? rel : null,
        status: 'queued', sent: 0, speed: 0, error: null, abort: null, startedAt: 0,
      });
    }
    this._emit();
    this._pump();
  }

  _get(id) { return this.items.find((i) => i.id === id); }

  pause(id) {
    const it = this._get(id);
    if (!it) return;
    if (it.status === 'uploading') { it.paused = true; it.abort?.(); }
    if (it.status === 'queued') it.status = 'paused';
    this._emit();
  }

  resume(id) {
    const it = this._get(id);
    if (!it || (it.status !== 'paused' && it.status !== 'error')) return;
    it.status = 'queued'; it.error = null; it.paused = false;
    this._emit();
    this._pump();
  }

  cancel(id) {
    const it = this._get(id);
    if (!it) return;
    it.cancelled = true;
    it.abort?.();
    if (it.uploadId) req(`/api/files/upload/${it.uploadId}`, { method: 'DELETE' }).catch(() => {});
    if (it.status !== 'done') it.status = 'cancelled';
    this._emit();
  }

  cancelAll() { this.items.filter((i) => ['queued', 'uploading', 'paused'].includes(i.status)).forEach((i) => this.cancel(i.id)); }
  pauseAll() { this.items.filter((i) => ['queued', 'uploading'].includes(i.status)).forEach((i) => this.pause(i.id)); }
  resumeAll() { this.items.filter((i) => ['paused', 'error'].includes(i.status)).forEach((i) => this.resume(i.id)); }

  clearFinished() {
    this.items = this.items.filter((i) => !['done', 'cancelled'].includes(i.status));
    this._emit();
  }

  _pump() {
    while (this.running < CONCURRENCY) {
      const next = this.items.find((i) => i.status === 'queued');
      if (!next) return;
      this.running++;
      next.status = 'uploading';
      next.startedAt = performance.now();
      this._emit();
      this._run(next).finally(() => { this.running--; this._emit(); this._pump(); });
    }
  }

  async _run(it) {
    let attempt = 0;
    while (true) {
      try {
        const init = await req('/api/files/upload/init', {
          method: 'POST',
          json: { dir: it.dir, name: it.name, size: it.size, mtime: it.file.lastModified || null, relative_path: it.relative },
        });
        it.uploadId = init.upload_id;
        let offset = init.offset;
        it.sent = offset;
        this._emit();
        const chunk = init.chunk_size || 8 * 1024 * 1024;
        while (offset < it.size) {
          const end = Math.min(offset + chunk, it.size);
          const res = await this._putChunk(it, offset, it.file.slice(offset, end));
          if (res.status === 409) { offset = res.expected; it.sent = offset; continue; }
          offset = res.offset;
          it.sent = offset;
          attempt = 0;
        }
        const entry = await req(`/api/files/upload/${it.uploadId}/complete`, { method: 'POST' });
        it.status = 'done'; it.sent = it.size; it.speed = 0; it.result = entry;
        this._emit();
        this.doneHooks.forEach((fn) => fn(it));
        return;
      } catch (err) {
        if (it.cancelled) return;
        if (it.paused) { it.status = 'paused'; it.speed = 0; return; }
        const fatal = err.status && err.status < 500 && err.status !== 408 && err.status !== 429;
        if (fatal || ++attempt > MAX_RETRIES) {
          it.status = 'error'; it.error = err.message || 'Upload failed'; it.speed = 0;
          return;
        }
        it.note = `Connection problem - retrying (${attempt}/${MAX_RETRIES})...`;
        this._emit();
        await sleep(Math.min(1000 * 2 ** attempt, 15000));
        it.note = null;
      }
    }
  }

  // One chunk. XHR rather than fetch: only XHR reports upload progress, which
  // is what makes the bar move smoothly instead of jumping 8 MB at a time.
  _putChunk(it, offset, blob) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', `/api/files/upload/${it.uploadId}?offset=${offset}`);
      const t = tokenOf();
      if (t) xhr.setRequestHeader('Authorization', `Bearer ${t}`);
      let lastT = performance.now();
      let lastLoaded = 0;
      xhr.upload.onprogress = (ev) => {
        it.sent = offset + ev.loaded;
        const now = performance.now();
        if (now - lastT > 500) {
          const inst = ((ev.loaded - lastLoaded) / (now - lastT)) * 1000;
          it.speed = it.speed ? it.speed * 0.6 + inst * 0.4 : inst;
          lastT = now; lastLoaded = ev.loaded;
        }
        this._emit();
      };
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText); } catch { /* empty */ }
        if (xhr.status === 409 && body.expected != null) return resolve({ status: 409, expected: body.expected });
        if (xhr.status >= 200 && xhr.status < 300) return resolve({ status: 200, offset: body.offset });
        const e = new Error(typeof body.detail === 'string' ? body.detail : `Upload failed (${xhr.status})`);
        e.status = xhr.status;
        reject(e);
      };
      xhr.onerror = () => reject(new Error('Network error'));
      xhr.onabort = () => reject(new Error('Paused'));
      it.abort = () => xhr.abort();
      xhr.send(blob);
    });
  }
}

export const uploads = new UploadEngine();

export function summarize(items) {
  const active = items.filter((i) => ['queued', 'uploading', 'paused', 'error'].includes(i.status));
  const total = items.filter((i) => i.status !== 'cancelled');
  const size = total.reduce((s, i) => s + i.size, 0);
  const sent = total.reduce((s, i) => s + Math.min(i.sent, i.size), 0);
  const speed = items.filter((i) => i.status === 'uploading').reduce((s, i) => s + (i.speed || 0), 0);
  const remaining = Math.max(0, size - sent);
  return {
    active: active.length,
    done: items.filter((i) => i.status === 'done').length,
    failed: items.filter((i) => i.status === 'error').length,
    total: total.length,
    size, sent, speed,
    pct: size ? Math.min(100, Math.round((sent / size) * 100)) : 100,
    eta: speed > 0 ? remaining / speed : NaN,
    working: items.some((i) => i.status === 'uploading' || i.status === 'queued'),
  };
}

// ------------------------------------------------------------------ dropped folders
// Like the media uploader's collector, but a file store keeps everything:
// empty files are real files, and an empty folder is still a folder.
const readBatch = (reader) => new Promise((res) => reader.readEntries((b) => res(b), () => res([])));

async function walk(entry, prefix, out) {
  if (!entry) return;
  if (entry.isFile) {
    const file = await new Promise((res) => entry.file(res, () => res(null)));
    if (file) out.files.push({ file, relativePath: prefix ? `${prefix}/${file.name}` : null });
    return;
  }
  if (entry.isDirectory) {
    const here = prefix ? `${prefix}/${entry.name}` : entry.name;
    const reader = entry.createReader();
    let batch = await readBatch(reader);
    let any = false;
    while (batch.length) {
      any = true;
      for (const c of batch) await walk(c, here, out);
      batch = await readBatch(reader);
    }
    if (!any) out.emptyDirs.push(here);
  }
}

/** Must be called synchronously inside the drop handler (entries expire on yield). */
export async function collectDrop(dt) {
  const out = { files: [], emptyDirs: [] };
  const items = dt?.items;
  const entries = [];
  if (items && items.length && typeof items[0].webkitGetAsEntry === 'function') {
    for (let i = 0; i < items.length; i++) {
      if (items[i].kind !== 'file') continue;
      const e = items[i].webkitGetAsEntry();
      if (e) entries.push(e);
    }
  }
  const loose = Array.from(dt?.files || []);
  if (entries.length) {
    for (const e of entries) await walk(e, '', out);
    if (out.files.length || out.emptyDirs.length) return out;
  }
  loose.forEach((f) => out.files.push({ file: f, relativePath: null }));
  return out;
}

/** Make a nested folder path, ignoring "already there". */
export async function ensureDirs(base, rels) {
  const made = new Set();
  for (const rel of rels) {
    let acc = base;
    for (const seg of rel.split('/').filter(Boolean)) {
      const next = joinPath(acc, seg);
      if (!made.has(next)) {
        try { await filesApi.mkdir(acc, seg); } catch { /* exists */ }
        made.add(next);
      }
      acc = next;
    }
  }
}
