import { useState } from 'react';
import { ChevronRight, ChevronDown, Folder, FolderOpen } from 'lucide-react';
import { cn } from '../../lib/utils';
import { hasFiles } from '../../lib/dropUpload';

function FolderNode({ node, depth, selectedId, onSelect, onDropMedia, onDropFiles, expanded, toggle, onContextMenu, marked }) {
  const [dragOver, setDragOver] = useState(false);
  const hasKids = (node.children || []).length > 0;
  const isOpen = expanded.has(node.id);
  const isSelected = selectedId === node.id;

  return (
    <div>
      <div
        onClick={(e) => onSelect(node.id, e)}
        onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); onContextMenu?.(e, node); }}
        onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault(); e.stopPropagation(); setDragOver(false);
          // Files dragged in from the desktop: upload them INTO this folder
          if (hasFiles(e.dataTransfer)) {
            onDropFiles?.(node.id, node.name, e.dataTransfer);
            return;
          }
          const mediaId = e.dataTransfer.getData('mediaId');
          let mediaIds = [];
          try { mediaIds = JSON.parse(e.dataTransfer.getData('mediaIds') || '[]'); } catch { /* single drag */ }
          if (mediaId || mediaIds.length) onDropMedia?.(node.id, mediaId, mediaIds);
        }}
        style={{ paddingLeft: `${depth * 12 + 8}px` }}
        className={cn(
          'group flex items-center gap-1 pr-2 py-1.5 rounded-md cursor-pointer transition text-sm select-none',
          isSelected ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200',
          marked?.has(node.id) && 'bg-[#ff5c1f]/15 text-orange-100 ring-1 ring-[#ff5c1f]/60',
          dragOver && 'ring-1 ring-red-500 bg-red-500/10'
        )}
        title={node.relative_path || node.name}
      >
        <button
          onClick={(e) => { e.stopPropagation(); if (hasKids) toggle(node.id); }}
          className={cn('w-4 h-4 flex items-center justify-center flex-shrink-0',
                        !hasKids && 'invisible')}
        >
          {isOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        </button>

        {isOpen && hasKids
          ? <FolderOpen className="w-4 h-4 flex-shrink-0 text-zinc-500" />
          : <Folder className="w-4 h-4 flex-shrink-0 text-zinc-500" />}

        <span className="truncate flex-1">{node.name}</span>
        <span className="text-[11px] text-zinc-600 flex-shrink-0 tabular-nums">
          {node.total_count || 0}
        </span>
      </div>

      {isOpen && hasKids && (
        <div>
          {node.children.map((child) => (
            <FolderNode
              key={child.id}
              node={child}
              depth={depth + 1}
              selectedId={selectedId}
              onSelect={onSelect}
              onDropMedia={onDropMedia}
              expanded={expanded}
              toggle={toggle}
              onContextMenu={onContextMenu}
              marked={marked}
              onDropFiles={onDropFiles}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function FolderTree({ tree = [], selectedId, onSelect, onDropMedia, onDropFiles, totalCount = 0, onContextMenu, marked }) {
  const [expanded, setExpanded] = useState(new Set());

  const toggle = (id) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  return (
    <div className="space-y-0.5">
      <div
        onClick={() => onSelect(null)}
        className={cn(
          'flex items-center gap-2 px-2 py-1.5 rounded-md cursor-pointer transition text-sm',
          selectedId === null ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200'
        )}
      >
        <Folder className="w-4 h-4 flex-shrink-0 text-zinc-500" />
        <span className="flex-1">All Media</span>
        <span className="text-[11px] text-zinc-600 tabular-nums">{totalCount}</span>
      </div>

      {tree.length === 0 && (
        <p className="px-2 py-3 text-xs text-zinc-600">
          No folders yet. Run the reindex to scan your media drive.
        </p>
      )}

      {tree.map((node) => (
        <FolderNode
          key={node.id}
          node={node}
          depth={0}
          selectedId={selectedId}
          onSelect={onSelect}
          onDropMedia={onDropMedia}
          expanded={expanded}
          toggle={toggle}
          onContextMenu={onContextMenu}
          marked={marked}
          onDropFiles={onDropFiles}
        />
      ))}
    </div>
  );
}
