import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronRight, Folder } from 'lucide-react';
import { cn } from '../../lib/utils';
import { filesApi, joinPath } from '../../lib/files';

import { DND_TYPE as DND } from '../../lib/files';

function Node({ node, depth, current, expanded, toggle, kids, onOpen, onDropItems, dropTarget, setDropTarget, onContext }) {
  const open = expanded.has(node.path);
  const active = current === node.path;
  const over = dropTarget === node.path;
  return (
    <div>
      <div
        data-drop-path={node.path}
        onContextMenu={(e) => { if (!onContext) return; e.preventDefault(); e.stopPropagation(); onContext(e, node); }}
        onDragOver={(e) => {
          if (![...e.dataTransfer.types].some((t) => t === DND || t === 'Files')) return;
          e.preventDefault(); e.dataTransfer.dropEffect = e.dataTransfer.types.includes(DND) ? 'move' : 'copy';
          setDropTarget(node.path);
        }}
        onDragLeave={() => setDropTarget((d) => (d === node.path ? null : d))}
        onDrop={(e) => { e.preventDefault(); e.stopPropagation(); setDropTarget(null); onDropItems(e, node.path); }}
        className={cn('group flex items-center rounded-lg pr-2 text-[13px] transition',
          active ? 'bg-accent/15 text-accent-hi' : 'text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-100',
          over && 'bg-accent/25 ring-1 ring-accent')}
        style={{ paddingLeft: 4 + depth * 14 }}>
        <button type="button" tabIndex={-1} aria-label={open ? 'Collapse' : 'Expand'} onClick={() => toggle(node)}
                className={cn('grid h-6 w-5 shrink-0 place-items-center text-zinc-600 hover:text-zinc-200', !node.has_children && 'invisible')}>
          <ChevronRight className={cn('h-3.5 w-3.5 transition-transform', open && 'rotate-90')} />
        </button>
        <button type="button" onClick={() => onOpen(node.path)} title={node.name}
                className="flex min-w-0 flex-1 items-center gap-2 py-1.5 text-left">
          <Folder className={cn('h-4 w-4 shrink-0', active ? 'fill-accent/30 text-accent' : 'fill-amber-300/20 text-amber-300/90')} strokeWidth={1.75} />
          <span className="truncate">{node.name}</span>
        </button>
      </div>
      {open && kids[node.path] && kids[node.path].map((k) => (
        <Node key={k.path} node={k} depth={depth + 1} current={current} expanded={expanded} toggle={toggle} kids={kids}
              onOpen={onOpen} onDropItems={onDropItems} dropTarget={dropTarget} setDropTarget={setDropTarget} onContext={onContext} />
      ))}
    </div>
  );
}

export default function FolderTree({ current, version, onOpen, onDropItems, onContext }) {
  const [kids, setKids] = useState({});
  const [expanded, setExpanded] = useState(new Set());
  const [dropTarget, setDropTarget] = useState(null);
  const loaded = useRef(new Set());

  const load = useCallback(async (p, force = false) => {
    if (loaded.current.has(p) && !force) return;
    loaded.current.add(p);
    try {
      const r = await filesApi.tree(p);
      setKids((k) => ({ ...k, [p]: r.folders }));
    } catch { loaded.current.delete(p); }
  }, []);

  // top level, and again whenever something changed underneath
  useEffect(() => {
    load('', true);
    expanded.forEach((p) => load(p, true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  // open the trail to wherever you are
  useEffect(() => {
    if (!current) return;
    const trail = [];
    let acc = '';
    current.split('/').filter(Boolean).slice(0, -1).forEach((seg) => { acc = joinPath(acc, seg); trail.push(acc); });
    if (!trail.length) return;
    setExpanded((prev) => { const n = new Set(prev); trail.forEach((t) => n.add(t)); return n; });
    trail.forEach((t) => load(t));
    const parents = ['', ...trail];
    parents.forEach((t) => load(t));
  }, [current, load]);

  const toggle = (node) => {
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(node.path)) n.delete(node.path); else { n.add(node.path); load(node.path); }
      return n;
    });
  };

  const top = kids[''];
  if (!top) return <div className="space-y-1.5 px-3 py-2">{[70, 55, 80].map((w) => <div key={w} className="h-5 animate-pulse rounded bg-zinc-800/70" style={{ width: `${w}%` }} />)}</div>;
  if (top.length === 0) return <p className="px-3 py-2 text-xs text-zinc-600">No folders yet.</p>;
  return (
    <div className="px-1.5">
      {top.map((n) => (
        <Node key={n.path} node={n} depth={0} current={current} expanded={expanded} toggle={toggle} kids={kids}
              onOpen={onOpen} onDropItems={onDropItems} dropTarget={dropTarget} setDropTarget={setDropTarget} onContext={onContext} />
      ))}
    </div>
  );
}
