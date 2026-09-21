import { useState, useEffect, useContext, useMemo, useRef} from 'react';
import { useVideos } from '../hooks/useVideos';
import TagSelector from '../components/shared/TagSelector';
import { DataContext } from '../context/DataContext';
import { ThemeContext } from '../context/ThemeContext';
import { apiCall } from '../lib/api';
import { cn } from '../lib/utils';
import { LayoutGrid, List, Sun, Moon, Play, Tag, Download, Trash2, Edit, Link, Plus, FolderOpen, ArrowUpDown, SlidersHorizontal, X, ChevronRight, Star, ImageDown } from 'lucide-react';
import { Dropdown } from '../components/files/Dialogs';
import MediaGrid from '../components/shared/MediaGrid';
import FileList from '../components/shared/FileList';
import VideoModal from '../components/shared/VideoModal';
import PhotoLightbox from '../components/shared/PhotoLightbox';
import PhotoProxyDialog from '../components/shared/PhotoProxyDialog';
import { collectDroppedFiles, hasFiles } from '../lib/dropUpload';
import ContextMenu from '../components/shared/ContextMenu';
import ConfirmDialog from '../components/shared/ConfirmDialog';
import BulkActionBar from '../components/shared/BulkActionBar';
import { canTranscribe, nounFor, titleNoun } from '../lib/mediaLabel';
import { useSearchParams } from 'react-router-dom';
import { setBrowsingType } from '../lib/mediaTypeStore';

// Anything that is not video, photo or audio is "a file": no rating, no
// notes, no transcript. Grouped under one tab rather than one per extension.
const NON_MEDIA = new Set(['document', 'project', 'other']);

function matchesMediaType(v, mediaType) {
  if (!mediaType || mediaType === 'all') return true;
  if (mediaType === 'files') return NON_MEDIA.has(v.media_type);
  return v.media_type === mediaType;
}

export default function BrowsePage({ sortBy, sortOrder, onSortByChange, onSortOrderChange }) {
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
  const [viewMode, setViewModeState] = useState(() => { try { return localStorage.getItem('zerko.library.view') === 'list' ? 'list' : 'grid'; } catch { return 'grid'; } });
  const setViewMode = (v) => { setViewModeState(v); try { localStorage.setItem('zerko.library.view', v); } catch { /* private mode */ } };
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filtersRef = useRef(null);
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
  const [proxyOpen, setProxyOpen] = useState(false);
  // tell the sidebar which tab is showing, so it lists only folders with that kind of media
  useEffect(() => { setBrowsingType(filter.mediaType); }, [filter.mediaType]);
  useEffect(() => () => setBrowsingType('all'), []);
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

  // window.confirm blocks the whole page and looks like a browser error. The
  // in-app dialog says the same thing without stopping the world.
  const [pendingDrop, setPendingDrop] = useState(null);

  const handleWindowFileDrop = async (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    setFileDragOver(false);
    const entries = await collectDroppedFiles(e.dataTransfer);
    if (!entries.length) return;
    setPendingDrop({ entries, folderId: projectFilter ?? null, folderName: currentFolderName });
  };

  const confirmDrop = () => {
    if (pendingDrop) dataContext.addToQueue(pendingDrop.entries, pendingDrop.folderId);
    setPendingDrop(null);
  };

  const visibleVideos = filteredVideos.filter((v) => { 
      const statusMatch = statusFilter === 'all' ? true : (v.status || 'raw') === statusFilter;
      const typeMatch = matchesMediaType(v, filter.mediaType);
      const projectMatch = projectFilter === null ? true
          : (includeSubfolders ? folderScope.has(v.folder_id) : v.folder_id === projectFilter); 
      const ratingMatch = ratingFilter === null ? true : (v.rating || 0) >= ratingFilter;
      const tagMatch = activeTags.length === 0
        ? true
        : activeTags.every((tid) => (v.tags || []).some((t) => t.id === tid));
      return statusMatch && typeMatch && projectMatch && ratingMatch && tagMatch;
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
    // The rubber-band box is gone: it kept appearing when you grabbed the
    // sidebar edge, and the dot on each tile (Shift, Ctrl, or a touch sweep)
    // does the selecting.
    return;
    // eslint-disable-next-line no-unreachable
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
        // 'files' is a grouping the interface invents - the server knows
        // document/project/other, so it is filtered on this side.
        media_type: (filter.mediaType && !['all', 'files'].includes(filter.mediaType))
          ? filter.mediaType : null
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
      const typeOk = matchesMediaType(v, filter.mediaType);
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

  // Downloading one file. The same signed-token dance the context menu does -
  // the stream endpoint will not serve a file on a bare session cookie.
  async function downloadFile(media) {
    try {
      const response = await apiCall(`/api/videos/${media.id}/download-token`);
      const a = document.createElement('a');
      a.href = `/api/video-file/${media.id}?token=${response.token}`;
      a.download = media.filename;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      setTimeout(() => document.body.removeChild(a), 1000);
      setToast('Download started…');
    } catch (err) {
      setToast('Could not download that file');
    }
  }

  async function handleItemClick(media) {
    // Opening a clip does not select it - selecting is the dot's job. The clip
    // does become the anchor, so a later Shift-click ranges from here.
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

  // Select one clip and make it the anchor for a later shift-click.
  function handleSelectOnly(media) {
    setSelectedVideoIds(new Set([media.id]));
    setLastSelectedId(media.id);
  }

  // Shift-click: everything between the anchor and this clip, in the order the
  // grid is showing them (displayVideos - which follows search and sorting -
  // not the unsorted list). The anchor stays put, so shift-clicking again
  // re-targets the range from the same starting point. Ctrl+Shift adds the
  // range to what is already selected instead of replacing it.
  function handleRangeSelect(media, additive = false) {
    const order = displayVideos.map((v) => v.id);
    let anchor = lastSelectedId;
    if (anchor === null || !order.includes(anchor)) {
      // nothing to start from yet: use the first thing already selected, else this one
      anchor = order.find((id) => selectedVideoIds.has(id)) ?? media.id;
      setLastSelectedId(anchor);
    }
    const a = order.indexOf(anchor);
    const b = order.indexOf(media.id);
    if (a === -1 || b === -1) { handleSelectOnly(media); return; }
    const range = order.slice(Math.min(a, b), Math.max(a, b) + 1);
    setSelectedVideoIds(new Set(additive ? [...selectedVideoIds, ...range] : range));
    window.getSelection?.()?.removeAllRanges?.();
  }

  // Touch sweep-select: press a tile's dot and drag over others. The first tile
  // decides whether the sweep adds or removes; everything between it and the
  // finger (in the order shown) gets that treatment.
  const sweepRef = useRef({ base: new Set(), mode: 'add', from: null });
  const selRef = useRef(selectedVideoIds);
  selRef.current = selectedVideoIds;
  const orderRef = useRef([]);
  orderRef.current = displayVideos.map((v) => v.id);
  useEffect(() => {
    const onSweep = (e) => {
      const { phase, id } = e.detail || {};
      const sw = sweepRef.current;
      const order = orderRef.current;
      if (phase === 'start') {
        sw.base = new Set(selRef.current);
        sw.mode = sw.base.has(id) ? 'remove' : 'add';
        sw.from = id;
        setLastSelectedId(id);
      } else if (sw.from === null) {
        return;
      }
      const a = order.indexOf(sw.from);
      const b = order.indexOf(id);
      if (a === -1 || b === -1) return;
      const range = order.slice(Math.min(a, b), Math.max(a, b) + 1);
      const next = new Set(sw.base);
      range.forEach((rid) => (sw.mode === 'add' ? next.add(rid) : next.delete(rid)));
      setSelectedVideoIds(next);
    };
    window.addEventListener('zerko-sweep-select', onSweep);
    return () => window.removeEventListener('zerko-sweep-select', onSweep);
  }, []);

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

  // Ctrl/Cmd+A selects everything shown, Esc clears, Enter opens a single selection.
  useEffect(() => {
    const onKey = (e) => {
      if (selectedVideoId || tagSelectorTarget || deleteTarget || renameTarget || createProjectOpen) return;
      if (e.target.closest && e.target.closest('input, textarea, select, [contenteditable="true"], [role="dialog"]')) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a' && filter.mediaType !== 'files') {
        e.preventDefault();
        setSelectedVideoIds(new Set(displayVideos.map((v) => v.id)));
      } else if (e.key === 'Escape' && selectedVideoIds.size) {
        setSelectedVideoIds(new Set());
        setLastSelectedId(null);
      } else if (e.key === 'Enter' && selectedVideoIds.size === 1 && filter.mediaType !== 'files') {
        const m = displayVideos.find((v) => v.id === [...selectedVideoIds][0]);
        if (m) handleItemClick(m);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  // Progress of a drag-to-folder move, broadcast by DataContext.moveVideosToFolder
  useEffect(() => {
    const prog = (e) => {
      const { done, total } = e.detail || {};
      if (total > 1) setToast(`Moving ${done} of ${total}...`);
      else setToast('Moving...');
    };
    const fin = (e) => {
      const { total, failed, error } = e.detail || {};
      if (failed) setToast(`Moved ${total - failed} of ${total}. ${error || 'Some could not be moved.'}`);
      else { setToast(`Moved ${total} ${total === 1 ? 'item' : 'items'}`); setSelectedVideoIds(new Set()); }
      setTimeout(() => setToast(null), failed ? 6000 : 2500);
    };
    window.addEventListener('zerko-move-progress', prog);
    window.addEventListener('zerko-move-done', fin);
    return () => { window.removeEventListener('zerko-move-progress', prog); window.removeEventListener('zerko-move-done', fin); };
  }, []);

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
      <ConfirmDialog
        open={!!pendingDrop}
        title="Upload these files?"
        message={pendingDrop
          ? `${pendingDrop.entries.length} file${pendingDrop.entries.length === 1 ? '' : 's'} into "${pendingDrop.folderName}".`
            + (pendingDrop.entries.filter((x) => x.relativePath).length
                ? ` ${pendingDrop.entries.filter((x) => x.relativePath).length} of them sit inside folders — that structure is recreated.`
                : '')
          : ''}
        onConfirm={confirmDrop}
        onCancel={() => setPendingDrop(null)}
      />

      {fileDragOver && (
        <div className="pointer-events-none absolute inset-0 z-40 flex items-center justify-center bg-zinc-950/80 backdrop-blur-sm">
          <div className="border-2 border-dashed border-accent rounded-2xl px-10 py-8 text-center bg-zinc-900/90">
            <p className="text-lg font-semibold text-zinc-100 mb-1">Drop to upload</p>
            <p className="text-sm text-zinc-400">
              into <span className="text-accent font-medium">{currentFolderName}</span>
            </p>
            <p className="text-xs text-zinc-600 mt-2">Folders keep their structure</p>
          </div>
        </div>
      )}
        {/* Selection Rectangle */}
        {dragState.start && (
            <div className="fixed border border-accent bg-accent/15 rounded-md pointer-events-none z-[100]"
                style={{
                    left: Math.min(dragState.start.x, dragState.current.x),
                    top: Math.min(dragState.start.y, dragState.current.y),
                    width: Math.abs(dragState.start.x - dragState.current.x),
                    height: Math.abs(dragState.start.y - dragState.current.y)
                }}
            />
        )}

        {/* One toolbar: what kind of thing, how it is ordered, and any filters. */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-zinc-800 px-6 py-3">
          <div className="flex items-center gap-0.5 rounded-xl bg-zinc-900 p-1" role="tablist" aria-label="Media type">
            {[['all', 'All'], ['video', 'Videos'], ['photo', 'Photos'], ['audio', 'Audio']].map(([type, label]) => (
              <button
                key={type}
                role="tab"
                aria-selected={filter.mediaType === type}
                onClick={() => setMediaTypeFilter(type)}
                className={cn('rounded-[9px] px-3.5 py-1.5 text-sm font-medium transition',
                  filter.mediaType === type ? 'bg-zinc-700 text-zinc-50 shadow-sm' : 'text-zinc-500 hover:text-zinc-200')}
              >
                {label}
              </button>
            ))}
          </div>

          {statusFilter !== 'all' && (
            <button onClick={() => setStatusFilter('all')} className="inline-flex items-center gap-1.5 rounded-full border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs capitalize text-accent hover:bg-accent/20">
              {statusFilter} <X className="h-3 w-3" />
            </button>
          )}
          {ratingFilter && (
            <button onClick={() => setRatingFilter(null)} className="inline-flex items-center gap-1 rounded-full border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs text-accent hover:bg-accent/20">
              <Star className="h-3 w-3 fill-current" /> {ratingFilter}+ <X className="h-3 w-3" />
            </button>
          )}

          <span className="ml-1 text-xs text-zinc-600">
            {visibleVideos.length} {visibleVideos.length === 1 ? 'item' : 'items'}
            {projectFilter !== null && includeSubfolders && folderScope.size > 1 && ` across ${folderScope.size} folders`}
          </span>

          <div className="ml-auto flex items-center gap-1.5">
            {filter.mediaType === 'photo' && (
              <button
                onClick={() => setProxyOpen(true)}
                className="inline-flex items-center gap-2 rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-sm font-medium text-accent transition hover:bg-accent/20 active:scale-95"
                title="Make 16:9 copies of photos and download them as a ZIP"
              >
                <ImageDown className="h-4 w-4" /> Generate proxy
              </button>
            )}
            <div className="relative" ref={filtersRef}>
              <button
                onClick={() => setFiltersOpen((o) => !o)}
                aria-expanded={filtersOpen}
                className={cn('inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm transition',
                  filtersOpen ? 'border-zinc-600 bg-zinc-800 text-zinc-100' : 'border-zinc-800 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100')}
              >
                <SlidersHorizontal className="h-4 w-4" /> Filters
                {(statusFilter !== 'all' ? 1 : 0) + (ratingFilter ? 1 : 0) > 0 && (
                  <span className="rounded-full bg-accent px-1.5 text-[10px] font-semibold leading-4 text-accent-foreground">
                    {(statusFilter !== 'all' ? 1 : 0) + (ratingFilter ? 1 : 0)}
                  </span>
                )}
              </button>
              {filtersOpen && (
                <>
                  <div className="fixed inset-0 z-30" onClick={() => setFiltersOpen(false)} />
                  <div className="absolute right-0 z-40 mt-2 w-72 rounded-xl border border-zinc-700/60 bg-zinc-900 p-4 shadow-2xl shadow-black/60">
                    <p className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-500">Status</p>
                    <div className="flex flex-wrap gap-1.5">
                      {STATUS_TABS.map((status) => (
                        <button key={status} onClick={() => setStatusFilter(status)}
                                className={cn('rounded-md px-2.5 py-1 text-xs capitalize transition',
                                  statusFilter === status ? 'bg-zinc-600 text-zinc-50' : 'bg-zinc-800 text-zinc-400 hover:text-zinc-200')}>
                          {status}
                        </button>
                      ))}
                    </div>
                    <p className="mb-2 mt-4 text-[11px] font-medium uppercase tracking-wider text-zinc-500">Minimum rating</p>
                    <div className="flex items-center gap-1">
                      {[1, 2, 3, 4, 5].map((rating) => (
                        <button key={rating} onClick={() => setRatingFilter(ratingFilter === rating ? null : rating)}
                                aria-label={`${rating} star${rating === 1 ? '' : 's'} or more`}
                                className={cn('text-xl transition', (ratingFilter || 0) >= rating ? 'text-yellow-500' : 'text-zinc-600 hover:text-zinc-400')}>★</button>
                      ))}
                    </div>
                    {(statusFilter !== 'all' || ratingFilter) && (
                      <button onClick={() => { setStatusFilter('all'); setRatingFilter(null); }} className="mt-4 text-xs text-zinc-400 hover:text-zinc-100">Reset filters</button>
                    )}
                  </div>
                </>
              )}
            </div>

            <Dropdown
              align="right"
              trigger={
                <button type="button" className="inline-flex items-center gap-2 rounded-lg border border-zinc-800 px-3 py-1.5 text-sm text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-100">
                  <ArrowUpDown className="h-4 w-4" />
                  <span className="capitalize">{sortBy}</span>
                  <span className="text-zinc-600">{sortOrder === 'asc' ? '↑' : '↓'}</span>
                </button>
              }
              items={[
                ...[['date', 'Date added'], ['name', 'Name'], ['size', 'Size'], ['duration', 'Duration']].map(([k, label]) => ({
                  label, active: sortBy === k, onClick: () => onSortByChange?.(k),
                })),
                { type: 'divider' },
                { label: 'Newest / largest first', active: sortOrder === 'desc', onClick: () => onSortOrderChange?.('desc') },
                { label: 'Oldest / smallest first', active: sortOrder === 'asc', onClick: () => onSortOrderChange?.('asc') },
              ]}
            />

            <div className="flex rounded-lg border border-zinc-800 p-0.5">
              {[['grid', LayoutGrid, 'Grid'], ['list', List, 'List']].map(([v, Ic, label]) => (
                <button key={v} onClick={() => setViewMode(v)} title={`${label} view`} aria-label={`${label} view`} aria-pressed={viewMode === v}
                        className={cn('rounded-md p-1.5 transition', viewMode === v ? 'bg-zinc-700 text-zinc-50' : 'text-zinc-500 hover:text-zinc-200')}>
                  <Ic className="h-4 w-4" />
                </button>
              ))}
            </div>

            <button onClick={() => setCreateProjectOpen(true)} title="Create a project (folder)" aria-label="Create a project"
                    className="rounded-lg border border-zinc-800 p-2 text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-100">
              <Plus className="h-4 w-4" />
            </button>
          </div>
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
        
        {projectFilter !== null && (
          <div className="flex items-center gap-1 border-b border-zinc-800/70 px-6 py-2 text-sm">
            <button onClick={() => setProjectFilter(null)} className="rounded px-2 py-1 text-zinc-500 transition hover:text-zinc-200">All media</button>
            {crumbs.map((c, i) => (
              <span key={c.id} className="flex items-center gap-1">
                <ChevronRight className="h-3.5 w-3.5 text-zinc-700" />
                <button onClick={() => setProjectFilter(c.id)}
                        className={cn('rounded px-2 py-1 transition', i === crumbs.length - 1 ? 'font-medium text-accent' : 'text-zinc-500 hover:text-zinc-200')}>
                  {c.name}
                </button>
              </span>
            ))}
          </div>
        )}

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
                    <span className="text-[10px] text-accent">Skipped: {batchJob.status.skipped}</span>
                    <span className="text-[10px] text-red-500">Failed: {batchJob.status.failed}</span>
                </div>
            </div>
        )}
        
        <div className="flex-1 overflow-y-auto p-6">
            {filter.mediaType === 'files' ? (
              <FileList
                items={displayVideos}
                loading={loading}
                folders={folders}
                selectedIds={selectedVideoIds}
                onToggleSelect={handleToggleSelect}
                onContextMenu={handleContextMenu}
                onOpen={downloadFile}
                onRename={(f) => setRenameTarget(f)}
                onMove={(f) => { setSelectedVideoIds(new Set([f.id])); setToast('Drag it onto a folder in the sidebar to move it'); }}
                onDelete={(f) => setDeleteTarget(f)}
              />
            ) : (
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
                onSelectOnly={handleSelectOnly}
                totalSelectedCount={selectedVideoIds.size}
                searchTerm={filter.search}
            />
            )}
        </div>
        
        {proxyOpen && (
          <PhotoProxyDialog
            onClose={() => setProxyOpen(false)}
            selectedPhotos={photoSet.filter((v) => selectedVideoIds.has(v.id))}
            viewPhotos={photoSet}
            folderTree={dataContext.folderTree || []}
            currentFolderId={selectedFolderId}
          />
        )}

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
