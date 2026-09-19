import { useState } from 'react';
import { Play } from 'lucide-react';
import StatusBadge from './StatusBadge';
import { API_BASE } from '../../lib/api';
import { cn } from '../../lib/utils';

export default function VideoTable({ videos = [], loading, onVideoClick, onContextMenu, onStatusChange, selectedVideoIds = new Set(), onToggleSelect, onRangeSelect }) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-zinc-500">
        <div className="animate-spin h-8 w-8 border-2 border-zinc-600 border-t-red-500 rounded-full" />
      </div>
    );
  }

  if (videos.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-zinc-500">
        <svg
          className="w-16 h-16 mb-4 text-zinc-700"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={1.5}
            d="M7 4v16M17 4v16M3 8h4m10 0h4M3 12h18M3 16h4m10 0h4M4 20h16a1 1 0 001-1V5a1 1 0 00-1-1H4a1 1 0 00-1-1H4a1 1 0 00-1 1v14a1 1 0 001 1z"
          />
        </svg>
        <p className="text-sm">No media found</p>
      </div>
    );
  }

  return (
    <div className="bg-zinc-900 rounded-lg border border-zinc-800 overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-zinc-800 text-zinc-500 text-xs uppercase tracking-wider">
            <th className="px-3 py-3 text-left font-medium w-16">Thumb</th>
            <th className="px-3 py-3 text-left font-medium">Filename</th>
            <th className="px-3 py-3 text-left font-medium w-24">Size</th>
            <th className="px-3 py-3 text-left font-medium w-20">Duration</th>
            <th className="px-3 py-3 text-left font-medium w-28">Status</th>
            <th className="px-3 py-3 text-left font-medium w-40">Tags</th>
            <th className="px-3 py-3 text-left font-medium w-28">Date</th>
          </tr>
        </thead>
        <tbody>
          {videos.map((video) => (
            <VideoTableRow
              key={video.id}
              video={video}
              onClick={onVideoClick}
              onContextMenu={onContextMenu}
              onStatusChange={onStatusChange}
              isSelected={selectedVideoIds.has(video.id)}
              onToggleSelect={onToggleSelect}
              onRangeSelect={onRangeSelect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function VideoTableRow({ video, onClick, onContextMenu, onStatusChange, isSelected, onToggleSelect, onRangeSelect }) {
  const [imageError, setImageError] = useState(false);

  const thumbnailUrl = video.thumbnail_path
    ? video.thumbnail_path.startsWith('http')
      ? video.thumbnail_path
      : `${API_BASE}${video.thumbnail_path}`
    : null;

  const formatDate = (dateStr) => {
    if (!dateStr) return '—';
    try {
      return new Date(dateStr).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return '—';
    }
  };

  const handleClick = (e) => {
    e.stopPropagation();
    if (e.ctrlKey || e.metaKey) {
      onToggleSelect?.(video);
    } else if (e.shiftKey) {
      onRangeSelect?.(video);
    } else {
      onClick?.(video);
    }
  };

  const handleContextMenu = (e) => {
    e.preventDefault();
    onContextMenu?.(e, video);
  };

  return (
    <tr
      data-id={video.id}
      className={cn(
        "border-b border-zinc-800/50 last:border-0 hover:bg-zinc-800/60 cursor-pointer transition",
        isSelected && "bg-blue-900/20 border-blue-500/30"
      )}
      onClick={handleClick}
      onContextMenu={handleContextMenu}
    >
      {/* Thumbnail */}
      <td className="px-3 py-2">
        {thumbnailUrl && !imageError ? (
          <img
            src={thumbnailUrl}
            alt={video.filename}
            className="w-12 h-8 rounded object-cover bg-zinc-800"
            onError={() => setImageError(true)}
          />
        ) : (
          <div className="w-12 h-8 rounded bg-zinc-800 flex items-center justify-center">
            <Play className="w-3.5 h-3.5 text-zinc-600" />
          </div>
        )}
      </td>

      {/* Filename */}
      <td className="px-3 py-2">
        <span className="text-zinc-100 font-medium truncate block max-w-xs">
          {video.filename}
        </span>
      </td>

      {/* Size */}
      <td className="px-3 py-2 text-zinc-400">
        {video.file_size_formatted || '—'}
      </td>

      {/* Duration */}
      <td className="px-3 py-2 text-zinc-400">
        {video.duration_formatted || '—'}
      </td>

      {/* Status */}
      <td className="px-3 py-2">
        <StatusBadge status={video.status} size="sm" />
      </td>

      {/* Tags */}
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-1">
          {(video.tags || []).slice(0, 2).map((tag) => (
            <span
              key={tag.id}
              className="text-xs px-1.5 py-0.5 bg-zinc-800 text-zinc-400 rounded"
            >
              {tag.name}
            </span>
          ))}
          {(video.tags?.length || 0) > 2 && (
            <span className="text-xs text-zinc-500">
              +{video.tags.length - 2}
            </span>
          )}
          {(video.tags?.length || 0) === 0 && (
            <span className="text-zinc-600 text-xs">—</span>
          )}
        </div>
      </td>

      {/* Date */}
      <td className="px-3 py-2 text-zinc-500 text-xs">
        {formatDate(video.uploaded_at)}
      </td>
    </tr>
  );
}
