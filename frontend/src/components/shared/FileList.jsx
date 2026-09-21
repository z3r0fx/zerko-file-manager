import { useMemo, useState } from 'react';
import {
  FileText, FileArchive, FileCode, FileImage, File as FileIcon, Box, Type,
  Download, Pencil, FolderInput, Trash2, ArrowUpDown,
} from 'lucide-react';

const EXT_ICON = [
  [/\.(pdf|doc|docx|odt|rtf|txt|md|pages|epub|log)$/i, FileText, 'text-sky-400'],
  [/\.(xls|xlsx|csv|ods|numbers)$/i, FileText, 'text-emerald-400'],
  [/\.(ppt|pptx|odp|key)$/i, FileText, 'text-amber-400'],
  [/\.(zip|rar|7z|tar|gz|bz2|xz|tgz|iso|dmg)$/i, FileArchive, 'text-violet-400'],
  [/\.(exe|msi|apk|appimage|deb|rpm|jar|bat|sh)$/i, FileCode, 'text-rose-400'],
  [/\.(psd|ai|eps|indd|sketch|fig|afphoto|afdesign)$/i, FileImage, 'text-fuchsia-400'],
  [/\.(ttf|otf|woff2?)$/i, Type, 'text-zinc-300'],
  [/\.(obj|fbx|stl|gltf|glb|3ds|dae|blend)$/i, Box, 'text-teal-400'],
  [/\.(drp|dra|drt|prproj|aep|aepx|fcpxml|edl|aaf|otio|veg|kdenlive)$/i, FileCode, 'text-orange-400'],
];

function iconFor(name) {
  for (const [re, Icon, colour] of EXT_ICON) if (re.test(name || '')) return [Icon, colour];
  return [FileIcon, 'text-zinc-400'];
}

const extOf = (n) => {
  const m = /\.([a-z0-9]+)$/i.exec(n || '');
  return m ? m[1].toUpperCase() : '';
};

const fmtSize = (b) => {
  if (!b) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; let n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};

const fmtDate = (s) => {
  if (!s) return '—';
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString(undefined,
    { year: 'numeric', month: 'short', day: 'numeric' });
};

/**
 * Documents and everything else that is not media.
 *
 * A PDF has no thumbnail worth looking at, no duration, no rating and nothing
 * to transcribe, so a grid of cards wastes a screen to say very little. This
 * is a file manager: name, kind, size, date, and the four things you actually
 * do to a file.
 */
export default function FileList({
  items = [], loading, folders = [],
  onOpen, onRename, onMove, onDelete, onContextMenu,
  selectedIds = new Set(), onToggleSelect,
  canOrganise = true, canDelete = true,
}) {
  const [sort, setSort] = useState({ key: 'date', dir: 'desc' });

  const folderName = useMemo(() => {
    const m = new Map();
    folders.forEach((f) => m.set(f.id, f.name));
    return (id) => m.get(id) || '—';
  }, [folders]);

  const rows = useMemo(() => {
    const out = [...items];
    const dir = sort.dir === 'asc' ? 1 : -1;
    out.sort((a, b) => {
      switch (sort.key) {
        case 'name': return dir * (a.filename || '').localeCompare(b.filename || '');
        case 'size': return dir * ((a.file_size || 0) - (b.file_size || 0));
        case 'kind': return dir * extOf(a.filename).localeCompare(extOf(b.filename));
        default: return dir * (new Date(a.uploaded_at || 0) - new Date(b.uploaded_at || 0));
      }
    });
    return out;
  }, [items, sort]);

  const toggleSort = (key) =>
    setSort((s) => ({ key, dir: s.key === key && s.dir === 'desc' ? 'asc' : 'desc' }));

  if (loading) {
    return <p className="py-16 text-center text-sm text-zinc-500">Loading…</p>;
  }

  if (!rows.length) {
    return (
      <div className="rounded-xl border border-dashed border-zinc-800 py-16 text-center">
        <FileIcon className="mx-auto mb-3 h-8 w-8 text-zinc-700" />
        <p className="text-zinc-400">No files here.</p>
        <p className="mt-1 text-sm text-zinc-600">
          Documents, archives and anything else that is not video, photo or audio shows up in this tab.
        </p>
      </div>
    );
  }

  const Head = ({ label, k, className = '' }) => (
    <th className={`px-3 py-2 text-left font-medium ${className}`}>
      <button onClick={() => toggleSort(k)}
              className="inline-flex items-center gap-1 text-zinc-400 transition hover:text-zinc-100">
        {label}
        <ArrowUpDown className={`h-3 w-3 ${sort.key === k ? 'text-accent' : 'text-zinc-700'}`} />
      </button>
    </th>
  );

  return (
    <div className="overflow-hidden rounded-xl border border-zinc-800">
      <table className="w-full text-sm">
        <thead className="bg-zinc-900/70 text-xs">
          <tr className="border-b border-zinc-800">
            <Head label="Name" k="name" />
            <Head label="Kind" k="kind" className="hidden sm:table-cell w-24" />
            <Head label="Size" k="size" className="w-28" />
            <Head label="Added" k="date" className="hidden md:table-cell w-32" />
            <th className="hidden px-3 py-2 text-left font-medium text-zinc-400 lg:table-cell">Folder</th>
            <th className="w-px px-3 py-2" />
          </tr>
        </thead>
        <tbody>
          {rows.map((f) => {
            const [Icon, colour] = iconFor(f.filename);
            const on = selectedIds.has(f.id);
            return (
              <tr
                key={f.id}
                onClick={(e) => (onToggleSelect ? onToggleSelect(f.id, e) : onOpen?.(f))}
                onDoubleClick={() => onOpen?.(f)}
                onContextMenu={(e) => onContextMenu?.(e, f)}
                className={
                  'cursor-default border-b border-zinc-800/70 transition last:border-0 ' +
                  (on ? 'bg-accent/10' : 'hover:bg-zinc-800/40')
                }
              >
                <td className="px-3 py-2">
                  <div className="flex min-w-0 items-center gap-2.5">
                    <Icon className={`h-4 w-4 shrink-0 ${colour}`} />
                    <span className="truncate text-zinc-200" title={f.filename}>{f.filename}</span>
                  </div>
                </td>
                <td className="hidden px-3 py-2 font-mono text-[11px] text-zinc-500 sm:table-cell">
                  {extOf(f.filename) || '—'}
                </td>
                <td className="px-3 py-2 font-mono text-[11px] text-zinc-400">{fmtSize(f.file_size)}</td>
                <td className="hidden px-3 py-2 text-[11px] text-zinc-500 md:table-cell">{fmtDate(f.uploaded_at)}</td>
                <td className="hidden truncate px-3 py-2 text-[11px] text-zinc-500 lg:table-cell">
                  {folderName(f.folder_id)}
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center justify-end gap-0.5">
                    <RowButton title="Download" onClick={() => onOpen?.(f)}><Download className="h-3.5 w-3.5" /></RowButton>
                    {canOrganise && (
                      <>
                        <RowButton title="Rename" onClick={() => onRename?.(f)}><Pencil className="h-3.5 w-3.5" /></RowButton>
                        <RowButton title="Move" onClick={() => onMove?.(f)}><FolderInput className="h-3.5 w-3.5" /></RowButton>
                      </>
                    )}
                    {canDelete && (
                      <RowButton title="Delete" danger onClick={() => onDelete?.(f)}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </RowButton>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RowButton({ title, onClick, children, danger = false }) {
  return (
    <button
      title={title}
      aria-label={title}
      onClick={(e) => { e.stopPropagation(); onClick?.(); }}
      className={
        'rounded-md border border-transparent p-1.5 text-zinc-500 transition ' +
        (danger
          ? 'hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-400'
          : 'hover:border-zinc-700 hover:bg-zinc-800 hover:text-zinc-100')
      }
    >
      {children}
    </button>
  );
}
