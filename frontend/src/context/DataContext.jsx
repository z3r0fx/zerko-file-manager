import { createContext, useState, useEffect, useContext, useCallback, useMemo, useRef } from 'react';
import { apiCall } from '../lib/api.js';
import { uploadFile } from '../lib/upload';
import { generateId } from '../lib/utils';

export const DataContext = createContext(null);

export function DataProvider({ children }) {
  const [videos, setVideos] = useState([]);
  const [tags, setTags] = useState([]);
  const [folders, setFolders] = useState([]);
  const [folderTree, setFolderTree] = useState([]);
  const [selectedFolderId, setSelectedFolderId] = useState(null);
  const [includeSubfolders, setIncludeSubfolders] = useState(true);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [uploadQueue, setUploadQueue] = useState([]);
  const [backgroundJobs, setBackgroundJobs] = useState({});
  const lastParams = useRef({});
  const reloadTimer = useRef(null);

  const loadVideos = useCallback(async (params = null) => {
    const finalParams = params !== null ? params : lastParams.current;
    lastParams.current = finalParams;
    const cleanParams = Object.fromEntries(
      Object.entries(finalParams).filter(([_, v]) => v !== null && v !== undefined && v !== 'null')
    );
    const qs = new URLSearchParams(cleanParams).toString();
    const data = await apiCall(`/api/videos${qs ? '?' + qs : ''}`);
    setVideos(data);
    return data;
  }, []);

  const loadTags = async (folderId = null) => {
    const url = folderId ? `/api/tags?folder_id=${folderId}` : '/api/tags';
    const data = await apiCall(url);
    setTags(data);
    return data;
  };

  const [tagTree, setTagTree] = useState([]);

  const loadTagTree = async (folderId = null) => {
    const url = folderId ? `/api/tags/tree?folder_id=${folderId}` : '/api/tags/tree';
    const data = await apiCall(url);
    setTagTree(data);
    return data;
  };

  const loadFolders = async () => {
    const data = await apiCall('/api/folders');
    setFolders(data);
    return data;
  };

  const loadFolderTree = async () => {
    const data = await apiCall('/api/folders/tree');
    setFolderTree(data);

    // If the selected folder no longer exists (deleted, or the tree was
    // rebuilt by a reorganise), fall back to All Media. Otherwise the grid
    // filters against a dead id and shows nothing at all.
    setSelectedFolderId((current) => {
      if (current == null) return current;
      let found = false;
      const walk = (nodes) => nodes.forEach((n) => {
        if (n.id === current) found = true;
        if (n.children) walk(n.children);
      });
      walk(data || []);
      return found ? current : null;
    });

    return data;
  };

  const loadStats = async () => {
    const data = await apiCall('/api/stats');
    setStats(data);
    return data;
  };

  const deleteVideo = async (id, deleteFile = true, permanent = false) => {
    // Default: move the file into your media folder's _Trash folder and keep the record, so it
    // can be restored. permanent=true destroys it and frees the space now.
    const qs = permanent ? '?permanent=true' : (deleteFile ? '?delete_file=true' : '');
    await apiCall(`/api/videos/${id}${qs}`, { method: 'DELETE' });
    setVideos((v) => v.filter((vid) => vid.id !== id));
    loadStats();
    loadFolders();
    loadFolderTree();
  };

  const transcribeVideo = async (id) => {
    const result = await apiCall(`/api/videos/${id}/transcribe`, { method: 'POST' });
    await reloadVideo(id);
    return result;
  };

  const updateVideoTags = async (id, tagIds) => {
    const result = await apiCall(`/api/videos/${id}/tags`, {
      method: 'POST',
      body: JSON.stringify({ tag_ids: tagIds }),
    });
    await reloadVideo(id);
    return result;
  };

  const removeVideoTag = async (id, tagId) => {
    const result = await apiCall(`/api/videos/${id}/tags/${tagId}`, { method: 'DELETE' });
    await reloadVideo(id);
    return result;
  };

  const reloadVideo = async (id) => {
    const updated = await apiCall(`/api/videos/${id}`);
    setVideos((v) => v.map((vid) => (vid.id === id ? { ...vid, ...updated } : vid)));
    return updated;
  };

  const createTag = async (name, parentId = null) => {
    const payload = { name };
    if (parentId) payload.parent_id = parentId;
    const result = await apiCall('/api/tags', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    await loadTags();
    await loadTagTree();
    return result;
  };

  const deleteTag = async (id) => {
    await apiCall(`/api/tags/${id}`, { method: 'DELETE' });
    setTags((t) => t.filter((tag) => tag.id !== id));
    await loadTags();
    await loadTagTree();
  };

  const createProject = async (name) => {
    const result = await apiCall('/api/folders', {
      method: 'POST',
      body: JSON.stringify({ name }),
    });
    await loadFolders();
    await loadFolderTree();
    return result;
  };

  const renameProject = async (id, name) => {
    await apiCall(`/api/folders/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ name }),
    });
    await loadFolderTree();
    await loadVideos();
    setFolders((f) =>
      f.map((folder) => (folder.id === id ? { ...folder, name } : folder))
    );
  };

  const deleteProject = async (id, deleteFiles = false) => {
    await apiCall(`/api/folders/${id}${deleteFiles ? '?delete_files=true' : ''}`, { method: 'DELETE' });
    await loadVideos();
    await loadStats();
    setFolders((f) => f.filter((folder) => folder.id !== id));
    await loadFolderTree();
  };

  const moveVideoToFolder = async (videoId, folderId) => {
    // The server physically moves the file, so the grid has to reload - the
    // clip's path and folder both changed.
    const result = await apiCall(`/api/videos/${videoId}/folder`, {
      method: 'POST',
      body: JSON.stringify({ folder_id: folderId }),
    });
    await loadVideos();
    await loadFolders();
    await loadFolderTree();
    return result;
  };

  const addNote = async (videoId, content) => {
    return await apiCall(`/api/videos/${videoId}/notes`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    });
  };

  const deleteNote = async (noteId) => {
    await apiCall(`/api/notes/${noteId}`, { method: 'DELETE' });
  };

  const updateVideoStatus = async (id, status) => {
    await apiCall(`/api/videos/${id}/status`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    });
    setVideos((v) => v.map((vid) => (vid.id === id ? { ...vid, status } : vid)));
  };

  const renameVideo = async (id, name) => {
    await apiCall(`/api/videos/${id}/rename`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    });
    await reloadVideo(id);
  };

  const updateVideoMetadata = async (id, metadata) => {
    setVideos((v) => v.map((vid) => (vid.id === id ? { ...vid, ...metadata } : vid)));
    await apiCall(`/api/videos/${id}/metadata`, {
      method: 'POST',
      body: JSON.stringify(metadata),
    });
  };

  const bulkAction = async (action, videoIds, extra = {}) => {
    return apiCall('/api/videos/bulk', {
      method: 'POST',
      body: JSON.stringify({
        action,
        video_ids: videoIds,
        tag_ids: extra.tag_ids,
        folder_id: extra.folder_id,
        status: extra.status,
      }),
    });
  };

  // SSE Listener for background jobs
  useEffect(() => {
    // A full-library transcribe fires ~1,100 'completed' events. Reloading
    // every video and the stats on each one meant 1,100 full refreshes of a
    // 1,216-item list - a large part of why the UI crawled.
    const scheduleReload = () => {
      if (reloadTimer.current) return;
      reloadTimer.current = setTimeout(() => {
        reloadTimer.current = null;
        loadVideos();
        loadStats();
      }, 4000);
    };

    const eventSource = new EventSource('/api/events');
    
    const handleJobEvent = (e) => {
        const data = JSON.parse(e.data);
        const job = data.job;
        
        setBackgroundJobs(prev => ({ ...prev, [job.job_id]: job }));

        if (job.status === 'completed' || job.status === 'failed') {
            scheduleReload();
        }
    };

    eventSource.addEventListener('job_added', handleJobEvent);
    eventSource.addEventListener('job_status', handleJobEvent);
    eventSource.addEventListener('job_progress', (e) => {
        const data = JSON.parse(e.data);
        setBackgroundJobs(prev => {
            if (!prev[data.job_id]) return prev;
            return {
                ...prev,
                [data.job_id]: { ...prev[data.job_id], progress: data.progress }
            };
        });
    });

    return () => {
      eventSource.close();
      if (reloadTimer.current) {
        clearTimeout(reloadTimer.current);
        reloadTimer.current = null;
      }
    };
  }, [loadVideos]);

  // Keep the job counters live from the DATABASE, not just from SSE events.
  // Events can be missed (tab opened late, stream reconnect, burst overflow);
  // the stats endpoint is authoritative and survives a server restart.
  useEffect(() => {
    // Nobody is looking at a hidden tab, so do not ask the server about it.
    const id = setInterval(() => {
      if (document.hidden) return;
      loadStats().catch(() => {});
    }, 5000);
    return () => clearInterval(id);
  }, []);

  // Upload Processing
  const activeUploads = useRef(0);

  useEffect(() => {
    const waiting = uploadQueue.filter(t => t.status === 'waiting');
    if (waiting.length > 0 && activeUploads.current < 4) {
      const nextTask = waiting[0];
      setUploadQueue(prev => prev.map(t => t.id === nextTask.id ? {...t, status: 'uploading'} : t));
      activeUploads.current++;
      uploadFile(nextTask.file, 
          (progress) => updateTask(nextTask.id, { progress }),
          (status) => updateTask(nextTask.id, { status }),
          nextTask.folderId ?? null,
          null,
          nextTask.relativePath ?? null
      ).then(async () => {
          await loadVideos();
          setUploadQueue(prev => prev.filter(t => t.id !== nextTask.id));
      }).catch(err => {
          updateTask(nextTask.id, { status: 'error', error: err.message });
      }).finally(() => {
          activeUploads.current--;
      });
    }
  }, [uploadQueue, loadVideos]);

  const addToQueue = (items, folderId = null) => {
    // Accepts either plain File objects (file picker) or
    // { file, relativePath } entries (drag-and-drop of folders).
    const newTasks = Array.from(items).map((item) => {
      const file = item && item.file ? item.file : item;
      const rel = (item && item.relativePath) || file.webkitRelativePath || null;
      return {
        id: generateId(),
        file,
        folderId: folderId ?? selectedFolderId,
        relativePath: rel,
        status: 'waiting',
        progress: 0,
        error: null,
      };
    });
    setUploadQueue((prev) => [...prev, ...newTasks]);
    return newTasks.length;
  };

  const updateTask = (id, updates) => {
    setUploadQueue(prev => prev.map(t => t.id === id ? {...t, ...updates} : t));
  };

  const retryUpload = (id) => {
    updateTask(id, { status: 'waiting', error: null, progress: 0 });
  };

  const removeFromQueue = (id) => {
    setUploadQueue(prev => prev.filter(t => t.id !== id));
  };

  useEffect(() => {
    async function loadInitialData() {
      setLoading(true);
      try {
        await Promise.all([loadVideos(), loadTags(), loadFolders(), loadFolderTree(), loadStats(), loadTagTree()]);
      } catch (err) {
        console.error('Failed to load initial data:', err);
      } finally {
        setLoading(false);
      }
    }
    loadInitialData();
  }, [loadVideos]);

  // Every folder id at or beneath the given one
  const folderDescendantIds = useCallback((folderId) => {
    const out = [];
    const walk = (nodes, collecting) => {
      for (const n of nodes) {
        const take = collecting || n.id === folderId;
        if (take) out.push(n.id);
        walk(n.children || [], take);
      }
    };
    walk(folderTree, false);
    return out;
  }, [folderTree]);

  // Path from the root down to the given folder, for breadcrumbs
  const folderBreadcrumbs = useCallback((folderId) => {
    let found = null;
    const walk = (nodes, trail) => {
      for (const n of nodes) {
        const next = [...trail, n];
        if (n.id === folderId) { found = next; return; }
        walk(n.children || [], next);
        if (found) return;
      }
    };
    walk(folderTree, []);
    return found || [];
  }, [folderTree]);

  const value = useMemo(() => ({
        videos, tags, folders, stats, loading, tagTree,
        folderTree, loadFolderTree,
        selectedFolderId, setSelectedFolderId,
        includeSubfolders, setIncludeSubfolders,
        folderDescendantIds, folderBreadcrumbs,
        loadVideos, loadTags, loadTagTree, loadFolders, loadStats,
        deleteVideo, transcribeVideo, updateVideoTags, removeVideoTag, reloadVideo,
        createTag, deleteTag, createProject, renameProject, deleteProject,
        moveVideoToFolder, updateVideoStatus, updateVideoMetadata, renameVideo,
        addNote, deleteNote, bulkAction, uploadQueue, backgroundJobs,
        addToQueue, retryUpload, removeFromQueue,
  }), [videos, tags, folders, folderTree, selectedFolderId, includeSubfolders,
       stats, loading, tagTree, uploadQueue, backgroundJobs]);

  return (
    <DataContext.Provider value={value}>
      {children}
    </DataContext.Provider>
  );
}

export function useData() {
  const context = useContext(DataContext);
  if (!context) {
    throw new Error('useData must be used within DataProvider');
  }
  return context;
}
