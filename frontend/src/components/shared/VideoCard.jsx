import { useState, useContext } from 'react';
import { Play, X, Download, FileText, Tag, FolderInput, Trash2, Check } from 'lucide-react';
import { cn } from '../../lib/utils';
import { DataContext } from '../../context/DataContext';
import StatusBadge from './StatusBadge';
import { API_BASE, getVideoStreamUrl } from '../../lib/api';
import { canTranscribe } from '../../lib/mediaLabel';

export default function VideoCard({ video, onClick, onContextMenu, onStatusChange, onTagClick, onMove, onDelete, onTagRemoved, isSelected, onToggleSelect, onRangeSelect, totalSelectedCount = 0 }) {
  const [imageError, setImageError] = useState(false);

  const handleContextMenu = (e) => {
    e.preventDefault();
    e.stopPropagation();
    onContextMenu?.(e, {
      ...video,
      menuItems: [
        { label: 'Play/Open', icon: Play, onClick: () => onClick?.(video) },
        ...(canTranscribe(video) ? [{ label: 'Transcribe', icon: FileText, onClick: () => onTranscribe?.(video) }] : []),
        { label: 'Tags', icon: Tag, onClick: () => onTagClick?.(video) },
        { 
          label: 'Download', 
          icon: Download, 
          onClick: () => {
            const link = document.createElement('a');
            link.href = getVideoStreamUrl(video.id);
            link.download = video.filename;
            link.click();
          } 
        },
        { type: 'divider' },
        { label: 'Move to Project', icon: FolderInput, onClick: () => onMove?.(video) },
        { label: 'Delete', icon: Trash2, danger: true, onClick: () => onDelete?.(video) },
      ]
    });
  };

  const handleClick = (e) => {
    if (e.ctrlKey || e.metaKey) {
      // Ctrl+click: toggle selection
      onToggleSelect?.(video);
    } else if (e.shiftKey) {
      // Shift+click: range select
      onRangeSelect?.(video);
    } else {
      // Normal click: select this video, deselect others
      onClick?.(video);
    }
  };

  const handleDragStart = (e) => {
    e.dataTransfer.setData('videoId', video.id);
    e.dataTransfer.effectAllowed = 'move';
    const count = totalSelectedCount > 1 ? totalSelectedCount : 1;
    e.dataTransfer.setData('videoCount', String(count));
  };

  const thumbnailUrl = video.thumbnail_path 
    ? (video.thumbnail_path.startsWith('http') ? video.thumbnail_path : `${API_BASE}${video.thumbnail_path}`)
    : null;

  return (
    <div
      data-id={video.id}
      className={cn(
        'bg-zinc-900 rounded-lg border border-zinc-800/60',
        'group relative cursor-pointer transition-all duration-200',
        'hover:border-red-500/40 hover:shadow-lg hover:shadow-red-500/5',
        isSelected && 'ring-2 ring-blue-500 ring-offset-2 ring-offset-zinc-900'
      )}
      draggable
      onDragStart={handleDragStart}
      onClick={handleClick}
      onContextMenu={handleContextMenu}
    >
      {/* Thumbnail */}
      <div className="aspect-video bg-zinc-800 relative overflow-hidden border-b border-zinc-800/60">
        {/* Selection Checkbox */}
        <div className={cn(
            "absolute top-2 left-2 z-20 transition-opacity duration-200",
            isSelected ? "opacity-100" : "opacity-0 group-hover:opacity-100"
        )}>
            <button 
                onClick={(e) => { e.stopPropagation(); onToggleSelect(video); }}
                className={cn(
                    "w-5 h-5 rounded border flex items-center justify-center transition-all",
                    isSelected ? "bg-red-600 border-red-600 text-white" : "bg-black/40 border-zinc-600 text-transparent"
                )}
            >
                <Check className="w-3.5 h-3.5" />
            </button>
        </div>
        {thumbnailUrl && !imageError ? (
          <img
            src={thumbnailUrl}
            alt={video.filename}
            className="w-full h-full object-cover"
            onError={() => setImageError(true)}
          />
        ) : (
          <div className="w-full h-full bg-gradient-to-br from-zinc-800 to-zinc-900 flex items-center justify-center">
            <Play className="w-8 h-8 text-zinc-600" />
          </div>
        )}


        {/* Hover Overlay */}
        <div className="absolute inset-0 bg-black/50 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-200 gap-3">
          <div className="w-12 h-12 rounded-full bg-white/20 backdrop-blur-sm flex items-center justify-center">
            <Play className="w-6 h-6 text-white fill-white" />
          </div>
        </div>
      </div>

      {/* Info Section */}
      <div className="p-3">
        <h3 className="text-sm font-medium text-zinc-100 truncate mb-2">
          {video.filename}
        </h3>

        {/* Meta Row */}
        <div className="flex items-center gap-2 flex-wrap mb-2">
          {video.color_flag && (
            <span className={cn("w-3 h-3 rounded-full", {
              'bg-red-500': video.color_flag === 'Red',
              'bg-yellow-500': video.color_flag === 'Yellow',
              'bg-green-500': video.color_flag === 'Green',
            })} title={video.color_flag} />
          )}
          {video.rating && (
            <span className="text-xs text-yellow-500">{'★'.repeat(video.rating)}</span>
          )}
          {video.is_selected && (
            <span className="text-xs font-bold text-blue-500">Selected</span>
          )}
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <StatusBadge 
            status={video.status} 
            onChange={(status) => onStatusChange?.(video.id, status)} 
          />
          {video.shoot_date && (
            <span className="text-xs text-zinc-500" title="Shoot Date">
              {new Date(video.shoot_date).toLocaleDateString()}
            </span>
          )}
        </div>

        {/* Tags */}
        {video.tags && video.tags.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {video.tags.slice(0, 3).map((tag) => (
              <span
                key={tag.id}
                className="group/tag relative text-xs px-1.5 py-0.5 bg-zinc-800 text-zinc-400 rounded flex items-center gap-1"
              >
                <TagPillRemove videoId={video.id} tag={tag} onRemoved={onTagRemoved} />
                {tag.name}
              </span>
            ))}
            {video.tags.length > 3 && (
              <span className="text-xs text-zinc-500">+{video.tags.length - 3}</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function TagPillRemove({ videoId, tag, onRemoved }) {
  const { removeVideoTag, loadVideos } = useContext(DataContext);
  const handleRemove = async (e) => {
    e.stopPropagation();
    try {
      await removeVideoTag(videoId, tag.id);
      await loadVideos();
      onRemoved?.();
    } catch (err) {
      console.error('Failed to remove tag:', err);
    }
  };
  return (
    <button
      onClick={handleRemove}
      className="hidden group-hover/tag:flex items-center justify-center w-3 h-3 rounded-full bg-zinc-700 hover:bg-red-500 text-zinc-400 hover:text-white transition-all text-xs leading-none"
    >
      <X className="w-2.5 h-2.5" />
    </button>
  );
}