import { useState, useEffect, useContext, useMemo, useRef} from 'react';
import { useVideos } from '../hooks/useVideos';
import TagSelector from '../components/shared/TagSelector';
import { DataContext } from '../context/DataContext';
import { ThemeContext } from '../context/ThemeContext';
import { apiCall } from '../lib/api';
import { cn } from '../lib/utils';
import { LayoutGrid, List, Sun, Moon, Play, Tag, Download, Trash2, Edit, Link, Plus, FolderOpen } from 'lucide-react';
import MediaGrid from '../components/shared/MediaGrid';
import VideoModal from '../components/shared/VideoModal';
import PhotoLightbox from '../components/shared/PhotoLightbox';
import { collectDroppedFiles, hasFiles } from '../lib/dropUpload';
import ContextMenu from '../components/shared/ContextMenu';
import ConfirmDialog from '../components/shared/ConfirmDialog';
import BulkActionBar from '../components/shared/BulkActionBar';
import { canTranscribe, nounFor, titleNoun } from '../lib/mediaLabel';
import { useSearchParams } from 'react-router-dom';

export default function BrowsePage({ sortBy, sortOrder }) {
  const dataContext = useContext(DataContext);
  const { videos, loading, loadVideos, updateVideoStatus, folders, createProject, moveVideoToFolder, loadFolders, renameVideo, tagTree, renameProject, deleteProject,
          selectedFolderId, setSelectedFolderId, includeSubfolders, folderDescendantIds, folderBreadcrumbs } = dataContext;
  const { theme, toggleTheme } = useContext(ThemeContext);
  const { filteredVideos, setMediaTypeFilter, setSearch, filter } = useVideos();
  // const [sortBy, setSortBy] = useState('date'); // Removed
  const [statusFilter, setStatusFilter] = useState('all');
  const projectFilter = selectedFolderId;
  const setProjectFilter = setSelectedFolderId;
  // const [sortOrder, setSortOrder] = useState('desc'); // Removed
  const [viewMode, setViewMode] = useState('grid');
  const [gridSize, setGridSize] = useState('medium');
  const [selectedVideoId, setSelectedVideoId] = useState(null);
  const [contextMenu, setContextMenu] = useState(null);
  const [tagSelectorTarget, setTagSelectorTarget] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [alsoDeleteFile, setAlsoDeleteFile] = useState(false);
  const [fileDragOver, setFileDragOver] = useState(false);


  // "Open folder on PC" opens Explorer on the machine running the server, so
  // only offer it when you're actually sitting at that machine.
  const isLocalServer = typeof window !== 'undefined' &&
    ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname);
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState(null);
  const [toast, setToast] = useState(null);
  const [batchJob, setBatchJob] = useState(null);
  const [selectedVideoIds, setSelectedVideoIds] = useState(new Set());
  const [lastSelectedId, setLastSelectedId] = useState(null);
  const [dragState, setDragState] = useState({ start: null, current: null });
  const [isDragging, setIsDragging] = useState(false);
  const [dragOverFolderId, setDragOverFolderId] = useState(null);

  // Compute derived state
  const STATUS_TABS = ['all', 'raw', 'edited', 'delivered', 'archived'];
  const statusCounts = videos.reduce((acc, v) => { const s = v.status || 'raw'; acc[s] = (acc[s] || 0) + 1; return acc; }, {});
  const projectCounts = folders.reduce((acc, f) => { acc[f.id] = videos.filter((v) => v.folder_id === f.id).length; return acc; }, {});
  
  const [ratingFilter, setRatingFilter] = useState(null);

  // Tag filtering, driven from the sidebar. An AND match on purpose: picking
  // "drone" and "sea view" should mean clips that are both, which is the
  // question worth asking of a library this size.
  const [activeTags, setActiveTags] = useState([]);
  useEffect(() => {
    const onTags = (e) => setActiveTags(Array.isArray(e.detail) ? e.detail : []);
    window.addEventListener('zerko-tag-filter', onTags);
    return () => window.removeEventListener('zerko-tag-filter', onTags);
  }, []);

  // Folder ids currently in scope (the selected folder plus its descendants)
  const folderScope = useMemo(
    () => new Set(projectFilter === null ? [] : folderDescendantIds(projectFilter)),
    [projectFilter, folderDescendantIds]
  );
  // Deep link from elsewhere in the app - "show me this clip". Used by the
  // client-picks panel so a pick takes you straight to the file rather than
  // leaving you to find it by name.
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkDone = useRef(false);

  useEffect(() => {
    if (deepLinkDone.current) return;
    const wantFolder = searchParams.get('folder');
    const wantVideo = searchParams.get('video');
    if (!wantFolder && !wantVideo) return;
    // Wait for the library to arrive, or we would open nothing.
    if (!videos || videos.length === 0) return;

    if (wantFolder) setSelectedFolderId?.(Number(wantFolder));
    if (wantVideo) {
      const id = Number(wantVideo);
      if (videos.some((v) => v.id === id)) {
        setSelectedVideoId(id);
      } else {
        setToast('That clip is no longer in the library');
        setTimeout(() => setToast(null), 4000);
      }
    }
    deepLinkDone.current = true;
    // Clear the params so a refresh does not keep re-opening the same clip.
    setSearchParams({}, { replace: true });
  }, [searchParams, videos, setSelectedFolderId, setSearchParams]);

  const crumbs = useMemo(
    () => (projectFilter === null ? [] : folderBreadcrumbs(projectFilter)),
    [projectFilter, folderBreadcrumbs]
  );

  // Dropping desktop files anywhere on the grid uploads them into the folder
  // you are currently looking at - the breadcrumb tells you exactly where.
  const currentFolderName = crumbs.length ? crumbs[crumbs.length - 1].name : 'All Media';

  const handleWindowFileDrop = async (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    setFileDragOver(false);
    const entries = await collectDroppedFiles(e.dataTransfer);
    if (!entries.length) return;
    const nested = entries.filter((x) => x.relativePath).length;
    const ok = window.confirm(
      `Upload ${entries.length} file${entries.length === 1 ? '' : 's'} into "${currentFolderName}"?` +
      (nested ? `\n\n${nested} of them are inside folders — that structure will be recreated.` : '')
    );
    if (ok) dataContext.addToQueue(entries, projectFilter ?? null);
  };

  const visibleVideos = filteredVideos.filter((v) => { 
      const statusMatch = statusFilter === 'all' ? true : (v.status || 'raw') === statusFilter; 
      const projectMatch = projectFilter === null ? true
          : (includeSubfolders ? folderScope.has(v.folder_id) : v.folder_id === projectFilter); 
      const ratingMatch = ratingFilter === null ? true : (v.rating || 0) >= ratingFilter;
      const tagMatch = activeTags.length === 0
        ? true
        : activeTags.every((tid) => (v.tags || []).some((t) => t.id === tid));
      return statusMatch && projectMatch && ratingMatch && tagMatch; 
  });

  // Drag-to-select logic
  useEffect(() => {
    const handleMouseMove = (e) => {
      if (!dragState.start) return;
      setIsDragging(true);
      setDragState(prev => ({ ...prev, current: { x: e.clientX, y: e.clientY } }));
      
      const rect = {
          left: Math.min(dragState.start.x, e.clientX),
          top: Math.min(dragState.start.y, e.clientY),
          right: Math.max(dragState.start.x, e.clientX),
          bottom: Math.max(dragState.start.y, e.clientY)
      };

      const elements = document.querySelectorAll('[data-id]');
      const nextSelection = new Set(e.ctrlKey || e.metaKey || e.shiftKey ? selectedVideoIds : []);
      
      elements.forEach(el => {
          const elRect = el.getBoundingClientRect();
          const intersects = !(
            elRect.right < rect.left || 
            elRect.left > rect.right || 
            elRect.bottom < rect.top || 
            elRect.top > rect.bottom
          );
          
          if (intersects) {
              const id = parseInt(el.getAttribute('data-id'));
              if (!isNaN(id)) {
                nextSelection.add(id);
              }
          }
      });
      
      setSelectedVideoIds(nextSelection);
    };
    const handleMouseUp = () => {
        document.body.style.userSelect = '';
        setDragState({ start: null, current: null });
        setTimeout(() => setIsDragging(false), 50);
    };
    if (dragState.start) {
        window.addEventListener('mousemove', handleMouseMove);
        window.addEventListener('mouseup', handleMouseUp);
    }
    return () => {
        window.removeEventListener('mousemove', handleMouseMove);
        window.removeEventListener('mouseup', handleMouseUp);
    };
  }, [dragState.start, visibleVideos, selectedVideoIds]);

  function handleMouseDown(e) {
    // No rubber-band selection while a video is open, a dialog is up, or the
    // click started on a control - there is nothing to marquee-select over.
    if (selectedVideoId || tagSelectorTarget || deleteTarget || renameTarget || createProjectOpen) return;
    if (e.target.closest('input, textarea, button, a, [contenteditable="true"]')) return;
    if (e.target.closest('[data-id]') || e.target.closest('button') || e.target.closest('a') || e.target.closest('input')) return;
    document.body.style.userSelect = 'none';
    setDragState({ start: { x: e.clientX, y: e.clientY }, current: { x: e.clientX, y: e.clientY } });
  }

  // Search listener
  useEffect(() => {
    const handleSearch = (e) => setSearch(e.detail);
    window.addEventListener('odyssey-search', handleSearch);
    return () => window.removeEventListener('odyssey-search', handleSearch);
  }, [setSearch]);

  // Load videos (debounced search + media type)
  const [transcriptMatches, setTranscriptMatches] = useState({});

  useEffect(() => {
    const handler = setTimeout(async () => {
      const search = filter.search;
      // 1. Always load standard video list
      loadVideos({ 
        sort_by: sortBy, 
        sort_order: sortOrder, 
        media_type: filter.mediaType !== 'all' ? filter.mediaType : null
      });

      // 2. If searching, also perform transcript search
      if (search) {
        try {
          const results = await apiCall(`/api/videos/transcript-search?q=${encodeURIComponent(search)}`);
          // results should be a map of video_id: [snippet, ...]
          setTranscriptMatches(results);
        } catch (err) {
          console.error("Transcript search failed", err);
          setTranscriptMatches({});
        }
      } else {
        setTranscriptMatches({});
      }
    }, 300);

    return () => clearTimeout(handler);
  }, [sortBy, sortOrder, filter.search, filter.mediaType, loadVideos]);

  // Derived state to merge filename and transcript matches
  const displayVideos = useMemo(() => {
    if (!filter.search) return visibleVideos;

    const searchTerm = filter.search.toLowerCase();

    // Everything except the text search - a transcript hit must survive these
    // but must NOT have to match by filename first. The old version filtered
    // transcript matches out of an already search-filtered list, so a clip that
    // only matched by something spoken in it could never appear.
    const passesFilters = (v) => {
      const statusOk = statusFilter === 'all' ? true : (v.status || 'raw') === statusFilter;
      const projectOk = projectFilter === null ? true
        : (includeSubfolders ? folderScope.has(v.folder_id) : v.folder_id === projectFilter);
      const ratingOk = ratingFilter === null ? true : (v.rating || 0) >= ratingFilter;
      const typeOk = (!filter.mediaType || filter.mediaType === 'all')
        ? true : v.media_type === filter.mediaType;
      return statusOk && projectOk && ratingOk && typeOk;
    };

    const byId = new Map();

    videos.forEach((v) => {
      if (v.filename?.toLowerCase().includes(searchTerm) && passesFilters(v)) {
        byId.set(v.id, { ...v, search_matches: transcriptMatches[v.id] || [] });
      }
    });

    Object.keys(transcriptMatches).forEach((key) => {
      const id = Number(key);
      const v = videos.find((x) => x.id === id);
      if (v && passesFilters(v)) {
        byId.set(id, { ...v, search_matches: transcriptMatches[id] || [] });
      }
    });

    // Spoken-word hits first - those are the ones you couldn't have found any
    // other way.
    return Array.from(byId.values()).sort(
      (a, b) => (b.search_matches?.length || 0) - (a.search_matches?.length || 0)
    );
  }, [visibleVideos, videos, filter.search, filter.mediaType, transcriptMatches,
      statusFilter, projectFilter, ratingFilter, includeSubfolders, folderScope]);
  
  const [openAtTime, setOpenAtTime] = useState(null);
  const [tagMoments, setTagMoments] = useState([]);

  // Photos open in the lightbox, not the video player. The set you tab through
  // is whatever photos are currently on screen, in the order you see them.
  const photoSet = useMemo(
    () => displayVideos.filter((v) => v.media_type === 'photo'),
    [displayVideos]
  );

  async function handleItemClick(media) {
    setSelectedVideoIds(new Set([media.id]));
    setLastSelectedId(media.id);

    // Searching and this clip has a spoken match? Open on the first one.
    const firstMatch = media.search_matches && media.search_matches[0];
    if (firstMatch) {
      setOpenAtTime(firstMatch.start);
      setSelectedVideoId(media.id);
      return;
    }

    // Filtering by a tag that came from the transcript? Open at the moment
    // they actually say it. Filtering for "pool" and landing at 00:00 of a
    // four-minute walkthrough makes you hunt for the thing you asked for.
    if (activeTags.length > 0) {
      const names = activeTags
        .map((id) => (dataContext?.tags || []).find((t) => t.id === id)?.name)
        .filter(Boolean);
      if (names.length) {
        try {
          const r = await apiCall(
            `/api/videos/${media.id}/tag-moments?tags=${encodeURIComponent(names.join(','))}`
          );
          const first = r?.moments?.[0];
          setTagMoments(r?.moments || []);
          setOpenAtTime(first ? first.start : null);
          setSelectedVideoId(media.id);
          return;
        } catch { /* fall through and just open it */ }
      }
    }

    setTagMoments([]);
    setOpenAtTime(null);
    setSelectedVideoId(media.id);
  }

  async function handleRename(id, newName) {
      await renameVideo(id, newName);
      setRenameTarget(null);
      await loadVideos();
  }

  function handleToggleSelect(media) {
    setSelectedVideoIds((prev) => {
      const next = new Set(prev);
      if (next.has(media.id)) next.delete(media.id); else next.add(media.id);
      return next;
    });
    setLastSelectedId(media.id);
  }

  function handleRangeSelect(media) {
    if (lastSelectedId === null) { handleToggleSelect(media); return; }
    const videoIds = visibleVideos.map((v) => v.id);
    const lastIdx = videoIds.indexOf(lastSelectedId);
    const currIdx = videoIds.indexOf(media.id);
    if (lastIdx === -1 || currIdx === -1) { handleToggleSelect(media); return; }
    const [start, end] = [Math.min(lastIdx, currIdx), Math.max(lastIdx, currIdx)];
    setSelectedVideoIds(new Set(videoIds.slice(start, end + 1)));
  }

  function handleContextMenu(e, media) {
    e.preventDefault();
    setContextMenu({
      x: e.clientX, y: e.clientY,
      items: [
        { label: 'Play/Open', icon: Play, onClick: () => { handleItemClick(media); setContextMenu(null); } },
        { label: 'Tags', icon: Tag, onClick: () => { setTagSelectorTarget(media); setContextMenu(null); } },
        { label: 'Show in library', icon: FolderOpen, onClick: () => {
            // Jump the folder tree to wherever this clip is filed
            if (media.folder_id != null) {
              setSelectedFolderId?.(media.folder_id);
              setSearch('');
            }
            setContextMenu(null);
        } },
        { label: 'Copy file path', icon: Link, onClick: async () => {
            setContextMenu(null);
            try {
              const loc = await apiCall(`/api/videos/${media.id}/location`);
              const p = loc.windows_path || loc.path || '';
              try { await navigator.clipboard.writeText(p); }
              catch {
                const ta = document.createElement('textarea');
                ta.value = p; document.body.appendChild(ta); ta.select();
                document.execCommand('copy'); document.body.removeChild(ta);
              }
              setToast(`Copied: ${p}`);
            } catch { setToast('Could not read the file location'); }
        } },
        ...(isLocalServer ? [{ label: 'Open folder on PC', icon: FolderOpen, onClick: async () => {
            setContextMenu(null);
            try {
              const r = await apiCall(`/api/videos/${media.id}/reveal`, { method: 'POST' });
              setToast(`Opened ${r.path}`);
            } catch (err) { setToast('Could not open Explorer — admin only, and only on the server PC'); }
        } }] : []),
        { label: 'Download', icon: Download, onClick: async () => { 
            try {
                const response = await apiCall(`/api/videos/${media.id}/download-token`);
                const dlLink = document.createElement("a");
                dlLink.href = `/api/video-file/${media.id}?token=${response.token}`;
                dlLink.download = media.filename;
                dlLink.style.display = "none";
                document.body.appendChild(dlLink);
                dlLink.click();
                setTimeout(() => document.body.removeChild(dlLink), 1000);
                setToast("Download started...");
                setTimeout(() => setToast(null), 3000);
            } catch (err) {
                console.error("Failed to get download token:", err);
                setToast("Failed to start download");
            }
            setContextMenu(null); 
        }},
        { label: 'Rename', icon: Edit, onClick: () => { setRenameTarget(media); setContextMenu(null); } },
        { label: 'Delete', icon: Trash2, danger: true, onClick: () => { setDeleteTarget(media); setContextMenu(null); } },
      ],
    });
  }

  async function handleBatchTranscribe() {
    // Stills and documents have no audio track - filter them out here rather
    // than making the backend reject them one by one.
    const videoIds = [...selectedVideoIds].filter((id) =>
      canTranscribe(videos.find((v) => v.id === id))
    );
    if (videoIds.length === 0) {
      setToast('Nothing in that selection can be transcribed.');
      setTimeout(() => setToast(null), 3000);
      return;
    }
    try {
        const response = await apiCall('/api/videos/batch-transcribe', { method: 'POST', body: JSON.stringify({ video_ids: videoIds }) });
        setBatchJob({ 
            id: response.job_id, 
            status: { 
                total: videoIds.length - (response.skipped || 0), 
                completed: 0, 
                failed: 0, 
                skipped: response.skipped || 0,
                in_progress: true 
            } 
        });
    } catch (err) {
        console.error("Batch transcribe error:", err);
    }
  }

  useEffect(() => {
    if (!batchJob?.id) return;
    const interval = setInterval(async () => {
        const statusList = await apiCall(`/api/videos/jobs/status`);
        const status = statusList.find(j => j.job_id === batchJob.id);
        if (status) {
            setBatchJob(prev => ({...prev, status}));
            if (!status.in_progress) { 
                clearInterval(interval); 
                loadVideos();
                setTimeout(() => setBatchJob(null), 2000); 
            }
        } else {
            clearInterval(interval);
            loadVideos();
            setBatchJob(null);
        }
    }, 2000);
    return () => clearInterval(interval);
  }, [batchJob, loadVideos]);

  const handleMainClick = (e) => {
    if (isDragging || e.target.closest('[data-id]') || e.target.closest('.bulk-action-bar')) return;
    setSelectedVideoIds(new Set());
    setLastSelectedId(null);
  };

  async function handleDropOnFolder(folderId, mediaId, mediaIds) {
      setDragOverFolderId(null);
      const ids = (mediaIds && mediaIds.length)
        ? mediaIds
        : (selectedVideoIds.has(Number(mediaId)) && selectedVideoIds.size > 1
            ? Array.from(selectedVideoIds)
            : [Number(mediaId)]);
      if (!ids.length) return;
      try {
        for (const id of ids) await moveVideoToFolder(id, folderId);
        setToast(`Moved ${ids.length} file${ids.length === 1 ? '' : 's'}`);
      } catch (err) {
        setToast(`Move failed: ${err.message || err}`);
      }
  }
  function handleDragOver(e, folderId) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setDragOverFolderId(folderId); }
  function handleDragLeave() { setDragOverFolderId(null); }

  return (
    <div
      className={cn("h-full flex flex-col overflow-hidden relative", theme === 'dark' ? 'dark bg-zinc-950 text-zinc-100' : 'bg-white text-zinc-900')}
      onMouseDown={handleMouseDown}
      onClick={handleMainClick}
      onDragOver={(e) => { if (hasFiles(e.dataTransfer)) { e.preventDefault(); setFileDragOver(true); } }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setFileDragOver(false); }}
      onDrop={handleWindowFileDrop}
    >
      {fileDragOver && (
        <div className="pointer-events-none absolute inset-0 z-40 flex items-center justify-center bg-zinc-950/80 backdrop-blur-sm">
          <div className="border-2 border-dashed border-[#ff5c1f] rounded-2xl px-10 py-8 text-center bg-zinc-900/90">
            <p className="text-lg font-semibold text-zinc-100 mb-1">Drop to upload</p>
            <p className="text-sm text-zinc-400">
              into <span className="text-[#ff5c1f] font-medium">{currentFolderName}</span>
            </p>
            <p className="text-xs text-zinc-600 mt-2">Folders keep their structure</p>
          </div>
        </div>
      )}
        {/* Selection Rectangle */}
        {dragState.start && (
            <div className="fixed border border-orange-500 bg-orange-500/15 rounded-md pointer-events-none z-[100]"
                style={{
                    left: Math.min(dragState.start.x, dragState.current.x),
                    top: Math.min(dragState.start.y, dragState.current.y),
                    width: Math.abs(dragState.start.x - dragState.current.x),
                    height: Math.abs(dragState.start.y - dragState.current.y)
                }}
            />
        )}

        {/* Media Types Filter */}
        <div className="flex items-center justify-between px-6 pt-6 pb-2 border-b border-zinc-800">
            <div className="flex items-center gap-4">
                {['all', 'video', 'photo', 'audio', 'document'].map((type) => (
                <button key={type} onClick={() => setMediaTypeFilter(type)} className={cn('capitalize text-sm font-medium transition', filter.mediaType === type ? 'text-red-400 border-b-2 border-red-500' : 'text-zinc-500 hover:text-zinc-300')}>{type}</button>
                ))}
            </div>
            <button onClick={() => setCreateProjectOpen(true)} className="flex items-center gap-2 text-sm text-zinc-400 hover:text-white transition">
                <Plus className="w-4 h-4" /> Create Project
            </button>
        </div>

        {/* Project Creation Dialog */}
        {createProjectOpen && (
            <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60" onClick={() => setCreateProjectOpen(false)}>
            <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 w-80 shadow-2xl" onClick={e => e.stopPropagation()}>
                <h3 className="text-lg font-semibold text-zinc-100 mb-4">Create New Project</h3>
                <input type="text" placeholder="Project Name" className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm mb-4" id="new-project-name" />
                <div className="flex justify-end gap-2">
                    <button onClick={() => setCreateProjectOpen(false)} className="px-3 py-1.5 text-zinc-400">Cancel</button>
                    <button onClick={async () => {
                        const name = document.getElementById('new-project-name').value;
                        if(name) { await createProject(name); setCreateProjectOpen(false); }
                    }} className="px-3 py-1.5 bg-red-600 text-white rounded">Create</button>
                </div>
            </div>
            </div>
        )}
        
        {/* Project Filter Bar */}
        <div className="flex flex-col gap-4 px-6 pt-4 pb-2 border-b border-zinc-800">
            <div className="flex items-center gap-2 text-sm min-h-[34px]">
                <button
                  onClick={() => setProjectFilter(null)}
                  className={cn('px-2 py-1 rounded transition', projectFilter === null ? 'text-red-400 font-medium' : 'text-zinc-500 hover:text-zinc-300')}
                >
                  All Media
                </button>

                {crumbs.map((c, i) => (
                  <span key={c.id} className="flex items-center gap-2">
                    <span className="text-zinc-700">/</span>
                    <button
                      onClick={() => setProjectFilter(c.id)}
                      className={cn('px-2 py-1 rounded transition',
                        i === crumbs.length - 1 ? 'text-red-400 font-medium' : 'text-zinc-500 hover:text-zinc-300')}
                    >
                      {c.name}
                    </button>
                  </span>
                ))}

                <span className="ml-auto text-xs text-zinc-600">
                  {visibleVideos.length} {visibleVideos.length === 1 ? 'item' : 'items'}
                  {projectFilter !== null && includeSubfolders && folderScope.size > 1 &&
                    ` across ${folderScope.size} folders`}
                </span>
            </div>

            <div className="flex items-center gap-4 text-sm pb-2">
                <div className="flex items-center gap-2">
                    <span className="text-zinc-500 text-xs uppercase">Status:</span>
                    {STATUS_TABS.map(status => (
                        <button key={status} onClick={() => setStatusFilter(status)} className={cn('px-2 py-1 rounded text-xs transition capitalize', statusFilter === status ? 'bg-zinc-700 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300')}>{status}</button>
                    ))}
                </div>
                <div className="flex items-center gap-2 border-l border-zinc-700 pl-4">
                    <span className="text-zinc-500 text-xs uppercase">Min Rating:</span>
                    <div className="flex gap-1">
                        {[1, 2, 3, 4, 5].map(rating => (
                            <button key={rating} onClick={() => setRatingFilter(ratingFilter === rating ? null : rating)} className={cn('text-lg transition', (ratingFilter || 0) >= rating ? 'text-yellow-500' : 'text-zinc-600') }>★</button>
                        ))}
                        {ratingFilter && <button onClick={() => setRatingFilter(null)} className="ml-2 text-xs text-zinc-500">Reset</button>}
                    </div>
                </div>
            </div>
        </div>

        {selectedVideoIds.size > 0 && (
            <BulkActionBar 
                selectedIds={Array.from(selectedVideoIds)} 
                onClear={() => setSelectedVideoIds(new Set())} 
                videos={videos}
                onBatchTranscribe={handleBatchTranscribe}
            />
        )}
        
        {deleteTarget && (
            <ConfirmDialog
                open={!!deleteTarget}
                title={`Delete ${deleteTarget.filename ? titleNoun(deleteTarget) : 'Project'}`}
                message={deleteTarget.filename
                  ? `Move this ${nounFor(deleteTarget)}, "${deleteTarget.filename}"${deleteTarget.file_size_formatted ? ` (${deleteTarget.file_size_formatted})` : ''}, to the trash? It goes to the _Trash folder on the server and you can restore it. Space is freed when you empty the trash.`
                  : `Delete the project "${deleteTarget.name}"? Files inside it stay on disk and become unfiled.`}
                extraOption={deleteTarget.filename ? {
                  checked: alsoDeleteFile,
                  onChange: setAlsoDeleteFile,
                  label: 'Skip the trash — delete permanently now',
                  hint: 'Frees the space immediately. Cannot be undone.',
                } : null}
                onCancel={() => { setDeleteTarget(null); setAlsoDeleteFile(false); }}
                onConfirm={async () => {
                    if(deleteTarget.filename) {
                        await deleteVideo(deleteTarget.id, true, alsoDeleteFile);
                    } else {
                        await deleteProject(deleteTarget.id);
                        await loadFolders();
                    }
                    setDeleteTarget(null); setAlsoDeleteFile(false);
                }}
            />
        )}

        {batchJob && (
            <div className="fixed bottom-20 right-6 w-72 bg-zinc-900 p-4 rounded-lg border border-zinc-800 shadow-xl z-30">
                <div className="flex items-center justify-between text-xs mb-1">
                    <span className="text-zinc-400">
                        {batchJob.status.in_progress ? `Transcribing... ${batchJob.status.completed + batchJob.status.failed}/${batchJob.status.total}` : 'Complete'}
                    </span>
                    <span className="text-zinc-500 font-mono">
                        {Math.round(((batchJob.status.completed + batchJob.status.failed) / batchJob.status.total) * 100)}%
                    </span>
                </div>
                <div className="w-full bg-zinc-800 h-2 rounded-full overflow-hidden border border-zinc-700/50">
                    <div 
                        className="bg-red-600 h-full transition-all duration-500 ease-out" 
                        style={{ width: `${batchJob.status.total > 0 ? ((batchJob.status.completed + batchJob.status.failed) / batchJob.status.total) * 100 : 100}%` }} 
                    />
                </div>
                <div className="flex gap-x-4 mt-2">
                    <span className="text-[10px] text-orange-500">Skipped: {batchJob.status.skipped}</span>
                    <span className="text-[10px] text-red-500">Failed: {batchJob.status.failed}</span>
                </div>
            </div>
        )}
        
        <div className="flex-1 overflow-y-auto p-6">
            <MediaGrid 
                mediaItems={displayVideos} 
                viewMode={viewMode} 
                gridSize={gridSize} 
                loading={loading} 
                onVideoClick={handleItemClick} 
                onContextMenu={handleContextMenu} 
                onStatusChange={updateVideoStatus}
                onTagClick={(media) => { setTagSelectorTarget(media); }}
                onMove={(media) => { /* Implement move handler */ }}
                onDelete={(media) => { setDeleteTarget(media); }}
                selectedVideoIds={selectedVideoIds} 
                onToggleSelect={handleToggleSelect} 
                onRangeSelect={handleRangeSelect} 
                totalSelectedCount={selectedVideoIds.size}
                searchTerm={filter.search}
            />
        </div>
        
        {selectedVideoId && (
            photoSet.length > 0 && photoSet.some(p => p.id === selectedVideoId) ? (
              <PhotoLightbox
                photos={photoSet}
                initialIndex={Math.max(0, photoSet.findIndex(p => p.id === selectedVideoId))}
                onClose={() => setSelectedVideoId(null)}
                onUpdateStatus={(id, s) => updateVideoStatus(id, s)}
              />
            ) : (
            <VideoModal 
                videoId={selectedVideoId} 
                startAt={openAtTime}
                tagMoments={tagMoments}
                highlightTerm={filter.search}
                onClose={() => { setSelectedVideoId(null); setOpenAtTime(null); }} 
                mediaList={visibleVideos}
                currentIndex={visibleVideos.findIndex(v => v.id === selectedVideoId)}
                onNavigate={(idx) => setSelectedVideoId(visibleVideos[idx].id)}
            />
            )
        )}
        {renameTarget && (
          <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60" onClick={() => setRenameTarget(null)}>
            <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 w-96 shadow-2xl" onClick={e => e.stopPropagation()}>
                <h3 className="text-lg font-semibold text-zinc-100 mb-4">{renameTarget.filename ? 'Rename File' : 'Rename Project'}</h3>
                <div className="flex items-center gap-2 mb-4">
                    <input 
                        type="text" 
                        defaultValue={renameTarget.filename ? renameTarget.filename.substring(0, renameTarget.filename.lastIndexOf('.')) : renameTarget.name} 
                        className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm" 
                        id="rename-input-name" 
                    />
                    {renameTarget.filename && (
                      <span className="text-zinc-500 text-sm">
                          {renameTarget.filename.substring(renameTarget.filename.lastIndexOf('.'))}
                      </span>
                    )}
                </div>
                <div className="flex justify-end gap-2">
                    <button onClick={() => setRenameTarget(null)} className="px-3 py-1.5 text-zinc-400">Cancel</button>
                    <button onClick={async () => {
                        const name = document.getElementById('rename-input-name').value;
                        if(renameTarget.filename) {
                          const ext = renameTarget.filename.substring(renameTarget.filename.lastIndexOf('.'));
                          await handleRename(renameTarget.id, name + ext);
                        } else {
                          await dataContext.renameProject(renameTarget.id, name);
                          setRenameTarget(null);
                          await dataContext.loadFolders();
                        }
                    }} className="px-3 py-1.5 bg-red-600 text-white rounded">Rename</button>

                </div>
            </div>
          </div>
        )}
        {tagSelectorTarget && (
          <TagSelector 
            video={tagSelectorTarget} 
            onClose={() => setTagSelectorTarget(null)} 
          />
        )}
        {contextMenu && <ContextMenu x={contextMenu.x} y={contextMenu.y} items={contextMenu.items} onClose={() => setContextMenu(null)} />}
        {toast && <div className="fixed bottom-4 left-1/2 -translate-x-1/2 bg-zinc-800 text-zinc-100 px-4 py-2 rounded-full shadow-lg z-[100] text-sm">{toast}</div>}
    </div>
  );
}
