import VideoCard from './VideoCard';

export default function VideoGrid({ videos = [], loading, onVideoClick, onContextMenu, onStatusChange, selectedVideoIds = new Set(), onToggleSelect, onRangeSelect, totalSelectedCount = 0 }) {
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
            d="M7 4v16M17 4v16M3 8h4m10 0h4M3 12h18M3 16h4m10 0h4M4 20h16a1 1 0 001-1V5a1 1 0 00-1-1H4a1 1 0 00-1 1v14a1 1 0 001 1z"
          />
        </svg>
        <p className="text-sm">No media found</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
      {videos.map((video) => (
        <VideoCard
          key={video.id}
          video={video}
          onClick={onVideoClick}
          onContextMenu={onContextMenu}
          onStatusChange={onStatusChange}
          isSelected={selectedVideoIds.has(video.id)}
          onToggleSelect={onToggleSelect}
          onRangeSelect={onRangeSelect}
          totalSelectedCount={totalSelectedCount}
        />
      ))}
    </div>
  );
}