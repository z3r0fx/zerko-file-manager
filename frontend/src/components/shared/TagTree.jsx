import { useState } from 'react';
import { ChevronRight, ChevronDown, Tag, Check, Plus, X as XIcon } from 'lucide-react';
import { cn } from '../../lib/utils';

export default function TagTree({ 
  tags, 
  selectedTagId, 
  onSelect, 
  tagCounts = {},
  selectedTagIds = [],  // For multi-select mode
  onDelete,             // Delete callback
  showActions = false   // Show delete action
}) {
  // Determine mode based on props
  const isMultiSelect = Array.isArray(selectedTagIds) && selectedTagIds.length > 0;
  
  return (
    <div className="space-y-1">
      {tags.map((tag) => (
        <TagTreeNode
          key={tag.id}
          tag={tag}
          selectedTagId={selectedTagId}
          selectedTagIds={selectedTagIds}
          onSelect={onSelect}
          tagCounts={tagCounts}
          depth={0}
          isMultiSelect={isMultiSelect}
          onDelete={onDelete}
          showActions={showActions}
        />
      ))}
    </div>
  );
}

function TagTreeNode({ 
  tag, 
  selectedTagId, 
  selectedTagIds, 
  onSelect, 
  tagCounts, 
  depth, 
  isMultiSelect,
  onDelete,
  showActions
}) {
  const [expanded, setExpanded] = useState(true);
  const hasChildren = tag.children && tag.children.length > 0;
  
  // Handle both single and multi-select modes
  let isSelected = false;
  if (isMultiSelect) {
    isSelected = selectedTagIds.includes(tag.id);
  } else if (selectedTagId !== undefined) {
    isSelected = selectedTagId === tag.id;
  }
  
  const count = tagCounts[tag.id] || 0;

  return (
    <div>
      <div
        className={cn(
          'group/tag flex items-center gap-2 px-4 py-2 rounded-lg transition text-left',
          isSelected 
            ? 'bg-red-500/20 text-red-400' 
            : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-100',
        )}
        style={{ paddingLeft: `${1 + depth * 0.75}rem` }}
      >
        {hasChildren ? (
          <button
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
            className="flex-shrink-0 cursor-pointer text-zinc-500 hover:text-zinc-300 p-0.5 -ml-0.5"
          >
            {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          </button>
        ) : (
          <span className="w-3.5 h-3.5 flex-shrink-0" />
        )}
        
        <button
          onClick={() => onSelect && onSelect(tag.id)}
          className="flex items-center gap-2 flex-1 min-w-0"
        >
          {isMultiSelect ? (
            isSelected ? (
              <Check className="w-3.5 h-3.5 flex-shrink-0 text-red-400" />
            ) : (
              <Plus className="w-3.5 h-3.5 flex-shrink-0" />
            )
          ) : null}
          <Tag className="w-3.5 h-3.5 flex-shrink-0" />
          <span className="text-sm truncate">{tag.name}</span>
          <span className="text-xs text-zinc-500">{count}</span>
        </button>
        
        {showActions && onDelete && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              if (confirm(`Delete tag "${tag.name}"?`)) {
                onDelete(tag.id);
              }
            }}
            className="hidden group-hover/tag:flex items-center justify-center w-5 h-5 rounded-full bg-zinc-700 hover:bg-red-500 text-zinc-400 hover:text-white transition-all text-xs leading-none flex-shrink-0"
            title="Delete tag"
          >
            <XIcon className="w-3 h-3" />
          </button>
        )}
      </div>
      {hasChildren && expanded && (
        <div className="mt-1">
          {tag.children.map((child) => (
            <TagTreeNode
              key={child.id}
              tag={child}
              selectedTagId={selectedTagId}
              selectedTagIds={selectedTagIds}
              onSelect={onSelect}
              tagCounts={tagCounts}
              depth={depth + 1}
              isMultiSelect={isMultiSelect}
              onDelete={onDelete}
              showActions={showActions}
            />
          ))}
        </div>
      )}
    </div>
  );
}