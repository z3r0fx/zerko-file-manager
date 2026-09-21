import { useState, useEffect, useContext, useMemo, useRef, useCallback } from 'react';
import { X as XIcon, Trash2, Tags, Mic, Loader2, Pencil, Check, X, ChevronLeft, ChevronRight } from 'lucide-react';
import { DataContext } from '../../context/DataContext';
import { apiCall } from '../../lib/api';
import { cn } from '../../lib/utils';
import StatusBadge from './StatusBadge';
import ConfirmDialog from './ConfirmDialog';
import TagSelector from './TagSelector';
import TagTree from './TagTree';
import NotesPanel from './NotesPanel';
import { nounFor, titleNoun, canTranscribe } from '../../lib/mediaLabel';

export default function VideoModal({ videoId, onClose, mediaList = [], currentIndex = -1, onNavigate, startAt = null, highlightTerm = '', tagMoments}) {
  const { videos, deleteVideo, transcribeVideo, renameVideo, updateVideoStatus, updateVideoTags, createTag, loadVideos, folders, moveVideoToFolder, loadFolders, updateVideoMetadata, reloadVideo } = useContext(DataContext);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [showTagSelector, setShowTagSelector] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [suggestedTags, setSuggestedTags] = useState([]);
  const [isEditingName, setIsEditingName] = useState(false);
  const [editName, setEditName] = useState('');
  const [useProxy, setUseProxy] = useState(true);
  const [streamUrl, setStreamUrl] = useState(null);
  const videoRef = useRef(null);
  // Segment start times that matched the tag you filtered by, so the
  // transcript shows WHY the clip jumped where it did.
  const tagMomentSet = new Set((tagMoments || []).map((m) => Math.round(m.start * 100)));

  // Sync video time with transcript
  const [currentTime, setCurrentTime] = useState(0);

  const video = useMemo(() => videos.find(v => v.id === videoId), [videos, videoId]);

  useEffect(() => {
    if (videoId) {
        reloadVideo(videoId);
    }
  }, [videoId]);

  const handleStatusChange = async (newStatus) => {
    try {
      await updateVideoStatus(video.id, newStatus);
      await loadVideos();
    } catch (err) {
      console.error('Failed to update status:', err);
    }
  };

  useEffect(() => {
    if (video) setEditName(video.filename);
  }, [video]);

  async function handleRename() {
    try {
      if (video && editName !== video.filename) await renameVideo(video.id, editName);
      setIsEditingName(false);
    } catch (err) {
      console.error('Failed to rename video', err);
    }
  }

  async function handleMetadataChange(key, value) {
    try {
      if (video) await updateVideoMetadata(video.id, { [key]: value });
    } catch (err) {
      console.error('Failed to update metadata', err);
    }
  }

  useEffect(() => {
    if (!videoId) {
      setStreamUrl(null);
      return;
    }
    const fetchToken = async () => {
      try {
        const data = await apiCall('/api/video-access-token');
        if (useProxy && video?.has_proxy) {
             setStreamUrl(`/api/video-proxy/${videoId}?token=${data.token}`);
        } else {
             setStreamUrl(`/api/video-proxy/${videoId}?token=${data.token}`);
        }
      } catch (error) {
        console.error("Error fetching video token:", error);
      }
    };
    fetchToken();
  }, [videoId, useProxy, video?.has_proxy]);

  useEffect(() => {
      const vid = videoRef.current;
      if (!vid) return;
      const onTimeUpdate = () => setCurrentTime(vid.currentTime);
      vid.addEventListener('timeupdate', onTimeUpdate);
      return () => vid.removeEventListener('timeupdate', onTimeUpdate);
  }, [streamUrl]);

  const seekTo = (time) => {
      if (videoRef.current) videoRef.current.currentTime = time;
  };

  // Opened from a transcript search hit: jump straight to the spoken moment.
  // Has to wait for loadedmetadata - setting currentTime before the browser
  // knows the duration is silently ignored.
  const seekTarget = useRef(null);
  useEffect(() => { seekTarget.current = startAt; }, [startAt, videoId]);

  useEffect(() => {
      const vid = videoRef.current;
      if (!vid || seekTarget.current == null) return undefined;
      const jump = () => {
          const t = seekTarget.current;
          if (t == null) return;
          try { vid.currentTime = Math.max(0, t - 0.4); } catch { /* not ready */ }
          seekTarget.current = null;
      };
      if (vid.readyState >= 1) jump();
      vid.addEventListener('loadedmetadata', jump);
      return () => vid.removeEventListener('loadedmetadata', jump);
  }, [streamUrl, videoId, startAt]);

  // Keyboard and navigation logic
  const navigate = useCallback((direction) => {
      if (!onNavigate || currentIndex === -1) return;
      const newIndex = currentIndex + direction;
      if (newIndex >= 0 && newIndex < mediaList.length) {
          onNavigate(newIndex);
      }
  }, [onNavigate, currentIndex, mediaList.length]);

  useEffect(() => {
    const handleKey = (e) => {
      if (document.activeElement?.tagName === 'INPUT' || document.activeElement?.tagName === 'TEXTAREA') return;
      const vid = videoRef.current;
      
      switch (e.key.toLowerCase()) {
        case 'arrowleft': navigate(-1); break;
        case 'arrowright': navigate(1); break;
        case 'j': e.preventDefault(); if (vid) vid.currentTime = Math.max(0, vid.currentTime - 10); break;
        case 'k': e.preventDefault(); if (vid) vid.paused ? vid.play() : vid.pause(); break;
        case 'l': e.preventDefault(); if (vid) vid.currentTime = Math.min(vid.duration, vid.currentTime + 10); break;
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [navigate]);

  useEffect(() => {
    const handleEscape = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [onClose]);

  const handleDelete = async () => {
    try {
      await deleteVideo(videoId);
      setShowDeleteConfirm(false);
      onClose();
    } catch (err) {
      console.error('Failed to delete video:', err);
    }
  };

  const handleTranscribe = async () => {
    setIsTranscribing(true);
    setSuggestedTags([]);
    try {
      await transcribeVideo(videoId);
      await loadVideos();
      const response = await apiCall(`/api/videos/${videoId}/suggest-tags`);
      setSuggestedTags(response);
    } catch (err) {
      console.error('Failed to transcribe video:', err);
    } finally {
      setIsTranscribing(false);
    }
  };

  const handleAddSuggestedTag = async (tagName) => {
      try {
        const newTag = await createTag(tagName, null);
        const currentTagIds = video.tags.map(t => t.id);
        await updateVideoTags(video.id, [...currentTagIds, newTag.id]);
        await loadVideos();
        setSuggestedTags(prev => prev.filter(t => t !== tagName));
      } catch (err) {
        console.error('Failed to add tag:', err);
      }
  }

  const handleAddAllSuggestedTags = async () => {
      try {
        const newTags = await Promise.all(suggestedTags.map(tagName => createTag(tagName, null)));
        const newTagIds = newTags.map(t => t.id);
        const currentTagIds = video.tags.map(t => t.id);
        await updateVideoTags(video.id, [...new Set([...currentTagIds, ...newTagIds])]);
        await loadVideos();
        setSuggestedTags([]);
      } catch (err) {
        console.error('Failed to add all tags:', err);
      }
  }

  if (!videoId) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/80 backdrop-blur-sm animate-shade-in" onClick={onClose} />
      <div className="relative bg-zinc-900 rounded-xl w-full max-w-5xl max-h-[90vh] overflow-hidden border border-zinc-700 shadow-2xl animate-pop-in">
        {!video ? (
          <div className="flex items-center justify-center h-96">
            <Loader2 className="w-8 h-8 text-red-500 animate-spin" />
          </div>
        ) : (
          <div className="flex flex-col lg:flex-row h-full max-h-[90vh]">
            <button onClick={onClose} className="absolute top-4 right-4 z-10 p-2 bg-zinc-800/80 rounded-lg text-zinc-400 hover:text-zinc-100 hover:bg-zinc-700 transition">
              <XIcon className="w-5 h-5" />
            </button>
            
            {/* Navigation Arrows */}
            {currentIndex > 0 && (
                <button onClick={() => navigate(-1)} className="absolute left-4 top-1/2 -translate-y-1/2 z-10 p-3 bg-zinc-800/80 rounded-full text-zinc-400 hover:text-zinc-100 hover:bg-zinc-700 transition">
                    <ChevronLeft className="w-6 h-6" />
                </button>
            )}
            {currentIndex < mediaList.length - 1 && (
                <button onClick={() => navigate(1)} className="absolute right-4 top-1/2 -translate-y-1/2 z-10 p-3 bg-zinc-800/80 rounded-full text-zinc-400 hover:text-zinc-100 hover:bg-zinc-700 transition">
                    <ChevronRight className="w-6 h-6" />
                </button>
            )}

            <div className="lg:w-[60%] bg-black flex items-center justify-center overflow-hidden">
              {/\.(jpe?g|png|gif|webp|bmp|svg)$/i.test(video.filename) ? (
                <img src={streamUrl} alt={video.filename} className="max-h-full max-w-full object-contain" />
              ) : (
                <video ref={videoRef} controls src={streamUrl} className="w-full" />
              )}
            </div>
            <div className="lg:w-[40%] p-6 overflow-y-auto border-t lg:border-t-0 lg:border-l border-zinc-800">
              {/* ... (rest of the panel content - identical to previous version) */}
              {/* NOTE: Including all code below to be complete */}
              <div className="flex items-center justify-between mb-4 pr-8">
                {isEditingName ? (
                    <div className="flex w-full gap-2">
                        <input type="text" value={editName} onChange={(e) => setEditName(e.target.value)} className="flex-1 bg-zinc-800 text-zinc-100 text-xl font-semibold px-2 py-1 rounded" />
                        <button onClick={handleRename} className="p-1 text-emerald-400"><Check className="w-5 h-5" /></button>
                        <button onClick={() => { setIsEditingName(false); setEditName(video.filename); }} className="p-1 text-red-400"><X className="w-5 h-5" /></button>
                    </div>
                ) : (
                    <>
                        <h2 className="text-xl font-semibold text-zinc-100 pr-2">{video.filename}</h2>
                        <button onClick={() => setIsEditingName(true)} className="p-1 text-zinc-400 hover:text-white"><Pencil className="w-4 h-4" /></button>
                    </>
                )}
              </div>
              <div className="mb-4">
                <h3 className="text-sm font-medium text-zinc-500 mb-2">Status</h3>
                <StatusBadge status={video.status} onChange={handleStatusChange} />
              </div>
              <div className="p-6 border border-zinc-800 rounded-lg mb-6">
                <h3 className="text-sm font-semibold text-zinc-300 mb-3">Rating</h3>
                <div className="flex gap-1 mb-4">
                    {[1, 2, 3, 4, 5].map((star) => (
                        <button key={star} onClick={() => handleMetadataChange('rating', star === video.rating ? 0 : star)} className={`text-xl ${star <= (video.rating || 0) ? 'text-yellow-500' : 'text-zinc-600'}`}>★</button>
                    ))}
                </div>
                <h3 className="text-sm font-semibold text-zinc-300 mb-3">Technical Metadata</h3>
                <div className="grid grid-cols-2 gap-2 text-xs text-zinc-400">
                  <p>Camera: <span className="text-zinc-200">{video.camera_make || 'N/A'} {video.camera_model || ''}</span></p>
                  <p>Codec: <span className="text-zinc-200">{video.video_codec || 'N/A'}</span></p>
                  <p>FPS: <span className="text-zinc-200">{video.frame_rate || 'N/A'}</span></p>
                  <p>Resolution: <span className="text-zinc-200">{video.resolution || 'N/A'}</span></p>
                  <p>Audio: <span className="text-zinc-200">{video.audio_codec || 'N/A'}</span></p>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4 mb-6 text-sm">
                <div><span className="text-zinc-500">Size</span><p className="text-zinc-300">{video.file_size_formatted}</p></div>
                <div><span className="text-zinc-500">Duration</span><p className="text-zinc-300">{video.duration_formatted}</p></div>
                {video.uploaded_at && (<div><span className="text-zinc-500">Uploaded</span><p className="text-zinc-300">{new Date(video.uploaded_at).toLocaleDateString()}</p></div>)}
                <div><h3 className="text-sm font-medium text-zinc-500 mb-1">Project</h3>
                  <select value={video.folder_id || ''} onChange={async (e) => { const f = e.target.value === '' ? null : Number(e.target.value); await moveVideoToFolder(video.id, f); await loadVideos(); await loadFolders(); }} className="w-full bg-zinc-800 text-zinc-300 text-sm rounded-lg px-3 py-2 border border-zinc-700">
                    <option value="">— None —</option>
                    {folders.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
                  </select>
                </div>
              </div>
              {video.transcription && (
                <div className="mb-6">
                  <div className="flex items-center justify-between mb-2">
                      <h3 className="text-sm font-medium text-zinc-500">Transcription</h3>
                      {video.has_proxy && (
                        <button onClick={() => setUseProxy(!useProxy)} className="text-xs text-blue-400 hover:text-blue-300">
                          {useProxy ? 'Switch to Original' : 'Switch to Proxy'}
                        </button>
                      )}
                  </div>
                  <div className="p-3 bg-zinc-800 rounded-lg text-xs text-zinc-300 h-60 overflow-y-auto space-y-2">
                    {video.segments?.map((seg, i) => {
                      const term = (highlightTerm || '').trim();
                      const isHit = term && seg.text?.toLowerCase().includes(term.toLowerCase());
                      const isNow = currentTime >= seg.start && currentTime <= seg.end;
                      const isTagMoment = tagMomentSet.has(Math.round(seg.start * 100));
                      return (
                        <div key={i} onClick={() => seekTo(seg.start)} className={cn(
                          "p-1.5 rounded cursor-pointer transition flex gap-2",
                          isNow ? "bg-accent/25 text-accent-hi"
                            : isHit ? "bg-yellow-500/10 hover:bg-yellow-500/20"
                            : isTagMoment ? "bg-emerald-500/10 ring-1 ring-emerald-500/30 hover:bg-emerald-500/20"
                            : "hover:bg-zinc-700")}>
                          <span className="font-mono text-[10px] text-zinc-500 shrink-0 pt-0.5 tabular-nums">
                            {String(Math.floor(seg.start / 60)).padStart(2, '0')}:{String(Math.floor(seg.start % 60)).padStart(2, '0')}
                          </span>
                          <span className="flex-1">
                            {isHit
                              ? seg.text.split(new RegExp(`(${term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi')).map((part, k) =>
                                  part.toLowerCase() === term.toLowerCase()
                                    ? <mark key={k} className="bg-yellow-400/30 text-yellow-100 rounded-sm px-0.5">{part}</mark>
                                    : <span key={k}>{part}</span>)
                              : seg.text}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
              <NotesPanel videoId={videoId} />
              
              {suggestedTags.length > 0 && (
                  <div className="p-3 bg-zinc-800 rounded-lg mb-4 text-sm">
                    <p className="text-zinc-400 mb-2">Suggested tags:</p>
                    <div className="flex flex-wrap gap-2 mb-3">
                      {suggestedTags.map(tag => (
                        <button key={tag} onClick={() => handleAddSuggestedTag(tag)} className="px-2 py-1 bg-zinc-700 text-zinc-200 rounded hover:bg-zinc-600 text-xs">
                          {tag}
                        </button>
                      ))}
                    </div>
                    <div className="flex gap-2 text-xs">
                      <button onClick={handleAddAllSuggestedTags} className="text-emerald-400 hover:text-emerald-300 font-medium">[Add All]</button>
                      <button onClick={() => setSuggestedTags([])} className="text-zinc-500 hover:text-zinc-300">[Dismiss]</button>
                    </div>
                  </div>
              )}

              <div className="flex flex-wrap gap-3 mt-6">
                {canTranscribe(video) && (
                <button onClick={handleTranscribe} disabled={isTranscribing || !!video.transcription} className="flex items-center gap-2 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-500 transition disabled:opacity-50 text-sm">
                  {isTranscribing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Mic className="w-4 h-4" />}
                  {isTranscribing ? 'Transcribing...' : 'Transcribe'}
                </button>
                )}
                <button onClick={() => setShowTagSelector(true)} className="flex items-center gap-2 px-4 py-2 bg-zinc-800 text-zinc-300 rounded-lg border border-zinc-700 transition text-sm">
                  <Tags className="w-4 h-4" /> Tags
                </button>
                <button onClick={() => setShowDeleteConfirm(true)} className="flex items-center gap-2 px-4 py-2 bg-transparent text-red-400 rounded-lg border border-red-500/30 transition text-sm">
                  <Trash2 className="w-4 h-4" /> Delete
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
      <ConfirmDialog open={showDeleteConfirm} title={`Delete ${titleNoun(video)}`} message={`Are you sure you want to delete this ${nounFor(video)} — "${video?.filename}"?`} onConfirm={handleDelete} onCancel={() => setShowDeleteConfirm(false)} />
      {showTagSelector && <TagSelector video={video} onClose={() => setShowTagSelector(false)} />}
    </div>
  );
}
