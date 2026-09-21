import { useState, useContext, useEffect } from 'react';
import { DataContext } from '../../context/DataContext';
import { Tag, FolderOpen, AlertCircle, Trash2, FileSpreadsheet, FileJson, Download, X, Mic, Link2 } from 'lucide-react';
import TagTree from './TagTree';
import ShareDialog from './ShareDialog';
import { countLabel, canTranscribe } from '../../lib/mediaLabel';

export default function BulkActionBar({ selectedIds, onClear, videos, onBatchTranscribe }) {
  const { tags, folders, tagTree, bulkAction, loadVideos, loadStats, loadFolders } = useContext(DataContext);
  const [active, setActive] = useState(null);
  const [selectedTagIds, setSelectedTagIds] = useState([]);
  const [selectedFolderId, setSelectedFolderId] = useState('');
  const [statusValue, setStatusValue] = useState('raw');
  const [shareOpen, setShareOpen] = useState(false);

  // Pre-populate values when panel opens or selection changes
  useEffect(() => {
    if (!active || selectedIds.length === 0) return;

    const selectedVideos = videos.filter((v) => selectedIds.includes(v.id));
    
    if (active === 'move') {
      const folderIds = [...new Set(selectedVideos.map(v => v.folder_id))];
      if (folderIds.length === 1) {
        setSelectedFolderId(folderIds[0] || '');
      } else {
        setSelectedFolderId('mixed');
      }
    } else if (active === 'status') {
      const statuses = [...new Set(selectedVideos.map(v => v.status || 'raw'))];
      if (statuses.length === 1) {
        setStatusValue(statuses[0]);
      } else {
        setStatusValue('mixed');
      }
    }
  }, [active, selectedIds, videos]);

  const count = selectedIds.length;
  if (count === 0) return null;

  const selectedVideos = videos.filter((v) => selectedIds.includes(v.id));

  const handleTag = async () => {
    await bulkAction('tag', selectedIds, { tag_ids: selectedTagIds });
    setActive(null);
    setSelectedTagIds([]);
    await loadVideos();
    onClear();
  };

  const handleUntag = async () => {
    await bulkAction('untag', selectedIds, { tag_ids: selectedTagIds });
    setActive(null);
    setSelectedTagIds([]);
    await loadVideos();
    onClear();
  };

  const handleMove = async () => {
    if (selectedFolderId === 'mixed') return;
    const fid = selectedFolderId === '' ? null : parseInt(selectedFolderId);
    await bulkAction('move', selectedIds, { folder_id: fid });
    setActive(null);
    setSelectedFolderId('');
    await loadVideos();
    await loadFolders();
    onClear();
  };

  const handleStatus = async () => {
    if (statusValue === 'mixed') return;
    await bulkAction('status', selectedIds, { status: statusValue });
    setActive(null);
    await loadVideos();
    onClear();
  };

  const handleDelete = async () => {
    await bulkAction('delete', selectedIds);
    setActive(null);
    await loadVideos();
    await loadStats();
    await loadFolders();
    onClear();
  };

  const exportCSV = () => {
    const headers = ['ID', 'Filename', 'Size', 'Duration', 'Status', 'Folder', 'Tags', 'Uploaded By', 'Uploaded At'];
    const rows = selectedVideos.map((v) => [
      v.id,
      `"${v.filename?.replace(/"/g, '""') || ''}"`,
      v.file_size_formatted || '',
      v.duration_formatted || '',
      v.status || 'raw',
      v.folder_name || '',
      (v.tags || []).map((t) => t.name).join('; '),
      v.uploaded_by || '',
      v.uploaded_at || '',
    ]);
    const csv = [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `oblivion_export_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const exportJSON = () => {
    const payload = selectedVideos.map((v) => ({
      id: v.id,
      filename: v.filename,
      filepath: v.filepath,
      file_size: v.file_size,
      file_size_formatted: v.file_size_formatted,
      duration: v.duration,
      duration_formatted: v.duration_formatted,
      status: v.status,
      folder_name: v.folder_name,
      folder_id: v.folder_id,
      tags: v.tags,
      uploaded_by: v.uploaded_by,
      uploaded_at: v.uploaded_at,
    }));
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `oblivion_export_${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const downloadFiles = () => {
    const token = localStorage.getItem('token');
    selectedVideos.forEach((v, i) => {
      setTimeout(() => {
        const url = `/api/video-file/${v.id}?token=${encodeURIComponent(token)}`;
        const a = document.createElement('a');
        a.href = url;
        a.download = v.filename || `video_${v.id}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      }, i * 300);
    });
  };

  // Only offer Transcribe when something in the selection can actually carry
  // audio - a pure stills selection has nothing to transcribe.
  const transcribable = selectedVideos.some(canTranscribe);

  const actionButtons = [
    { id: 'tag', label: 'Tag', icon: Tag },
    ...(transcribable ? [{ id: 'transcribe', label: 'Transcribe', icon: Mic }] : []),
    { id: 'move', label: 'Move', icon: FolderOpen },
    { id: 'status', label: 'Status', icon: AlertCircle },
    { id: 'delete', label: 'Delete', icon: Trash2 },
    { id: 'download', label: 'Download', icon: Download },
    { id: 'share', label: 'Share', icon: Link2 },
  ];
  
  const handleTranscribeAction = () => {
      console.log("BulkActionBar: Transcribe button clicked, calling onBatchTranscribe");
      onBatchTranscribe?.();
      setActive(null);
  };

  return (
    <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-30 bulk-action-bar">
      <div className="bg-zinc-900 border border-zinc-700 rounded-xl shadow-2xl flex flex-col items-center overflow-hidden">
        <div className="flex items-center gap-1 px-3 py-2">
          <span className="text-xs text-zinc-400 mr-2">{countLabel(selectedVideos, count)} selected</span>
          {actionButtons.map((btn) => {
            const Icon = btn.icon;
            return (
              <button
                key={btn.id}
                onClick={(e) => {
                  e.stopPropagation();
                  if (btn.id === 'download') { downloadFiles(); return; }
                  if (btn.id === 'transcribe') { handleTranscribeAction(); return; }
                  if (btn.id === 'share') { setShareOpen(true); return; }
                  setActive(active === btn.id ? null : btn.id);
                }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition bg-zinc-800 hover:bg-zinc-700 text-zinc-200"
              >
                <Icon className="w-3.5 h-3.5" />
                {btn.label}
              </button>

            );
          })}
          <button
            onClick={(e) => {
              e.stopPropagation();
              onClear();
            }}
            className="ml-1 p-1.5 rounded-lg text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800 transition"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Expandable panels */}
        {active === 'tag' && (
          <div className="w-full border-t border-zinc-700 px-4 py-3 max-h-64 overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <p className="text-xs text-zinc-400 mb-2">Select tags to apply:</p>
            <TagTree
              tags={tagTree}
              selectedTagId={selectedTagIds[0] || null}
              onSelect={(id) => {
                setSelectedTagIds((prev) =>
                  prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
                );
              }}
              tagCounts={{}}
            />
            <div className="flex gap-2 mt-3">
              <button onClick={handleTag} className="px-3 py-1.5 bg-accent-lo hover:bg-accent text-white text-xs rounded-lg font-medium transition">Apply Tags</button>
              <button onClick={handleUntag} className="px-3 py-1.5 bg-zinc-700 hover:bg-zinc-600 text-zinc-200 text-xs rounded-lg font-medium transition">Remove Tags</button>
              <button onClick={() => setActive(null)} className="px-3 py-1.5 text-zinc-400 hover:text-zinc-200 text-xs transition">Cancel</button>
            </div>
          </div>
        )}

        {active === 'move' && (
          <div className="w-full border-t border-zinc-700 px-4 py-3" onClick={(e) => e.stopPropagation()}>
            <p className="text-xs text-zinc-400 mb-2">Move to project:</p>
            <select
              value={selectedFolderId}
              onChange={(e) => setSelectedFolderId(e.target.value)}
              className="w-full bg-zinc-800 border border-zinc-700 text-zinc-200 text-sm rounded-lg px-3 py-2"
            >
              {selectedFolderId === 'mixed' && <option value="mixed">Mixed Projects (choose to override)</option>}
              <option value="">No project</option>
              {(folders || []).map((f) => (
                <option key={f.id} value={f.id}>{f.name}</option>
              ))}
            </select>
            <div className="flex gap-2 mt-3">
              <button 
                onClick={handleMove} 
                disabled={selectedFolderId === 'mixed'}
                className="px-3 py-1.5 bg-accent-lo hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs rounded-lg font-medium transition"
              >
                Move
              </button>
              <button onClick={() => setActive(null)} className="px-3 py-1.5 text-zinc-400 hover:text-zinc-200 text-xs transition">Cancel</button>
            </div>
          </div>
        )}

        {active === 'status' && (
          <div className="w-full border-t border-zinc-700 px-4 py-3" onClick={(e) => e.stopPropagation()}>
            <p className="text-xs text-zinc-400 mb-2">Set status:</p>
            <select
              value={statusValue}
              onChange={(e) => setStatusValue(e.target.value)}
              className="w-full bg-zinc-800 border border-zinc-700 text-zinc-200 text-sm rounded-lg px-3 py-2"
            >
              {statusValue === 'mixed' && <option value="mixed">Mixed Statuses (choose to override)</option>}
              <option value="raw">Raw</option>
              <option value="edited">Edited</option>
              <option value="delivered">Delivered</option>
              <option value="archived">Archived</option>
            </select>
            <div className="flex gap-2 mt-3">
              <button 
                onClick={handleStatus} 
                disabled={statusValue === 'mixed'}
                className="px-3 py-1.5 bg-accent-lo hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs rounded-lg font-medium transition"
              >
                Set Status
              </button>
              <button onClick={() => setActive(null)} className="px-3 py-1.5 text-zinc-400 hover:text-zinc-200 text-xs transition">Cancel</button>
            </div>
          </div>
        )}

        {active === 'delete' && (
          <div className="w-full border-t border-zinc-700 px-4 py-3" onClick={(e) => e.stopPropagation()}>
            <p className="text-xs text-red-400 mb-2">Permanently delete {countLabel(selectedVideos, count)}?</p>
            <div className="flex gap-2">
              <button onClick={handleDelete} className="px-3 py-1.5 bg-red-600 hover:bg-red-500 text-white text-xs rounded-lg font-medium transition">Delete Permanently</button>
              <button onClick={() => setActive(null)} className="px-3 py-1.5 text-zinc-400 hover:text-zinc-200 text-xs transition">Cancel</button>
            </div>
          </div>
        )}
      </div>

      {shareOpen && (
        <ShareDialog
          videoIds={selectedIds}
          onClose={() => setShareOpen(false)}
        />
      )}
    </div>
  );
}