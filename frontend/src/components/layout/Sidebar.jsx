import { useState, useContext, useEffect } from 'react';
import TagTree from '../shared/TagTree';
import FolderTree from '../shared/FolderTree';
import JobsPanel from '../shared/JobsPanel';
import ContextMenu from '../shared/ContextMenu';
import PromptDialog from '../shared/PromptDialog';
import ConfirmDialog from '../shared/ConfirmDialog';
import TrashPanel from '../shared/TrashPanel';
import ShareDialog from '../shared/ShareDialog';
import { apiCall } from '../../lib/api';
import { collectDroppedFiles } from '../../lib/dropUpload';
import { cn } from '../../lib/utils';
import { useNavigate, useLocation } from 'react-router-dom';
import { Home, LayoutDashboard, Upload, Settings, LogOut, ChevronDown, ChevronRight, HardDrive, ListFilter, FolderTree as FolderTreeIcon, Activity, Edit, Trash2, Link2, Copy, Tags as TagsIcon } from 'lucide-react';
import { AuthContext } from '../../context/AuthContext';
import { DataContext } from '../../context/DataContext';
import { useVideos } from '../../hooks/useVideos';

export default function Sidebar({ sortBy, sortOrder, onSortByChange, onSortOrderChange }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout } = useContext(AuthContext);
  const dataContext = useContext(DataContext);
  const { filter } = useVideos();
  
  const videos = dataContext?.videos || [];
  const folderTree = dataContext?.folderTree || [];
  const selectedFolderId = dataContext?.selectedFolderId ?? null;
  const setSelectedFolderId = dataContext?.setSelectedFolderId;
  const includeSubfolders = dataContext?.includeSubfolders ?? true;
  const setIncludeSubfolders = dataContext?.setIncludeSubfolders;
  const moveVideoToFolder = dataContext?.moveVideoToFolder;

  const tagTree = dataContext?.tagTree || [];
  const stats = dataContext?.stats;

  // Jobs still queued or running - drives the pulsing indicator.
  // Sourced from /api/stats (database-backed) rather than the SSE stream, so
  // it is right even if this tab connected after the work was queued.
  const jobBuckets = stats?.jobs || {};
  const outstanding = (bucket) =>
    ((bucket && bucket.queued) || 0) + ((bucket && bucket.processing) || 0);
  const jobsRunning = outstanding(jobBuckets.transcription) + outstanding(jobBuckets.proxy);

  const [tagsExpanded, setTagsExpanded] = useState(true);
  const [foldersExpanded, setFoldersExpanded] = useState(true);
  const [jobsOpen, setJobsOpen] = useState(false);
  const [folderMenu, setFolderMenu] = useState(null);
  const [renameFolder, setRenameFolder] = useState(null);
  const [deleteFolder, setDeleteFolder] = useState(null);
  const [alsoDeleteFolderFiles, setAlsoDeleteFolderFiles] = useState(false);
  const [folderError, setFolderError] = useState(null);
  const [trashOpen, setTrashOpen] = useState(false);
  const [shareFolder, setShareFolder] = useState(null);
  const [dropBusy, setDropBusy] = useState(null);

  // Files dragged from the desktop straight onto a folder in the tree.
  // The upload is pinned to THAT folder, so it cannot land somewhere else.
  const handleDropFiles = async (folderId, folderName, dataTransfer) => {
    setDropBusy(folderName);
    try {
      const entries = await collectDroppedFiles(dataTransfer);
      if (!entries.length) return;
      const nested = entries.filter((e) => e.relativePath).length;
      const ok = window.confirm(
        `Upload ${entries.length} file${entries.length === 1 ? '' : 's'} into "${folderName}"?` +
        (nested ? `\n\n${nested} of them are inside folders — that structure will be recreated.` : '')
      );
      if (ok) dataContext?.addToQueue?.(entries, folderId);
    } finally {
      setDropBusy(null);
    }
  };
  const [marked, setMarked] = useState(new Set());
  const [mergeOpen, setMergeOpen] = useState(false);
  const [merging, setMerging] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const [pwCurrent, setPwCurrent] = useState('');
  const [pwNew, setPwNew] = useState('');
  const [pwMsg, setPwMsg] = useState(null);

  const toggleMark = (id) => setMarked((prev) => {
    const next = new Set(prev);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });

  // Names of the marked folders, for the merge dialog
  const markedNames = [];
  (function collect(nodes) {
    (nodes || []).forEach((n) => {
      if (marked.has(n.id)) markedNames.push(n.name);
      collect(n.children);
    });
  })(folderTree);
  const [adminExpanded, setAdminExpanded] = useState(false);
  // Multiple tags at once: "drone" AND "sea view" is the query that actually
  // gets asked, not one tag at a time.
  const [activeTags, setActiveTags] = useState([]);
  const [tagQuery, setTagQuery] = useState('');

  const getTagCounts = () => {
    const counts = {};
    videos.forEach((video) => {
      video.tags?.forEach((tag) => {
        counts[tag.id] = (counts[tag.id] || 0) + 1;
      });
    });
    return counts;
  };

  const tagCounts = getTagCounts();

  // A flat list ordered by how many clips carry each tag. With 60+ tags a
  // nested tree buries the useful ones; "which tags do I actually have, and
  // how much is in them" is the question being asked.
  const allTags = (dataContext?.tags || [])
    .slice()
    .sort((a, b) => (tagCounts[b.id] || 0) - (tagCounts[a.id] || 0)
                 || (a.name || '').localeCompare(b.name || ''));
  const currentView = location.pathname.split('/')[1] || 'browse';

  const handleNav = (path) => navigate(path);

  const emitTags = (ids) => {
    setActiveTags(ids);
    // BrowsePage listens for this. Previously the event was dispatched and
    // nothing anywhere listened, so clicking a tag did literally nothing.
    window.dispatchEvent(new CustomEvent('zerko-tag-filter', { detail: ids }));
  };

  const handleTagSelect = (tagId) => {
    if (tagId == null) { emitTags([]); return; }
    emitTags(activeTags.includes(tagId)
      ? activeTags.filter((t) => t !== tagId)
      : [...activeTags, tagId]);
  };

  const handleLogout = () => logout();

  return (
    <aside className="w-64 h-screen bg-zinc-950 border-r border-zinc-800 flex flex-col flex-shrink-0">
      <div className="p-6 pb-4">
        <h1 className="flex items-center gap-2.5 select-none">
          <span
            aria-hidden="true"
            className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-gradient-to-br from-[#ff7a45] to-[#e03e00] shadow-lg shadow-[#ff5c1f]/20"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="#0b0b0d" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
              <rect x="2" y="5" width="14" height="14" rx="2" />
              <path d="M16 10l6-3v10l-6-3" />
            </svg>
          </span>
          <span className="flex flex-col leading-none">
            <span className="text-[17px] font-bold tracking-tight bg-gradient-to-r from-[#ff7a45] to-[#ff5c1f] bg-clip-text text-transparent">
              ZERKO
            </span>
            <span className="text-[10px] font-medium uppercase tracking-[0.22em] text-zinc-400 mt-0.5">
              File Manager
            </span>
          </span>
        </h1>
      </div>

      <div className="p-3 mx-3 mb-2 bg-zinc-800 rounded-lg border border-zinc-700/50">
        <div className="flex items-center gap-2 mb-1.5">
          <HardDrive className="w-3.5 h-3.5 text-zinc-500" />
          <span className="text-xs text-zinc-400">Storage</span>
        </div>
        <div className="flex items-baseline justify-between mb-2">
          <span className="text-xs text-zinc-300">{stats?.total_storage_formatted ?? '0 B'}</span>
          <span className="text-xs text-zinc-500">{videos?.length ?? 0} files</span>
        </div>
      </div>

      <nav className="px-3 space-y-1">
        <button onClick={() => handleNav('/browse')} className={cn("w-full flex items-center gap-3 px-4 py-2.5 rounded-lg transition", currentView === 'browse' ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-100')}>
          <Home className="w-5 h-5" />
          <span className="font-medium">Browse</span>
        </button>

        {user?.role === 'admin' && (
          <div className="mt-2">
            <button onClick={() => setAdminExpanded(!adminExpanded)} className="w-full flex items-center justify-between px-4 py-2.5 text-zinc-400 hover:text-zinc-100 transition">
              <div className="flex items-center gap-3"><Settings className="w-5 h-5" /><span className="font-medium">Admin</span></div>
              {adminExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            </button>
            {adminExpanded && (
              <div className="pl-6 space-y-1">
                  <button onClick={() => handleNav('/dashboard')} className={cn("w-full flex items-center gap-3 px-4 py-2 rounded-lg transition text-sm", currentView === 'dashboard' ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-100')}>
                      <LayoutDashboard className="w-4 h-4" /> Dashboard
                  </button>
                  <button onClick={() => handleNav('/admin')} className={cn("w-full flex items-center gap-3 px-4 py-2 rounded-lg transition text-sm", currentView === 'admin' ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-100')}>
                      <Settings className="w-4 h-4" /> User Management
                  </button>
              </div>
            )}
          </div>
        )}

        <button
          type="button"
          onClick={() => setJobsOpen((v) => !v)}
          aria-expanded={jobsOpen}
          className={cn(
            'w-full flex items-center justify-between px-4 py-2.5 rounded-lg border transition mt-1',
            'hover:bg-zinc-800/50',
            jobsOpen && 'bg-zinc-800 text-zinc-100',
            jobsRunning > 0
              ? 'job-pulse border-zinc-700 text-zinc-100'
              : 'border-transparent text-zinc-500 hover:text-zinc-200'
          )}
          title={jobsRunning > 0 ? `${jobsRunning} job${jobsRunning === 1 ? '' : 's'} in progress` : 'No jobs running — click to view'}
        >
          <div className="flex items-center gap-3">
            <span className="relative flex items-center justify-center w-5 h-5">
              <Activity className="w-4 h-4" />
              {jobsRunning > 0 && (
                <span className="job-dot-pulse absolute -right-0.5 -top-0.5 w-2 h-2 rounded-full bg-[#ff5c1f]" />
              )}
            </span>
            <span className="font-medium text-sm">Jobs</span>
          </div>
          {jobsRunning > 0 && (
            <span className="text-[11px] font-mono bg-zinc-800 text-zinc-200 px-2 py-0.5 rounded">
              {jobsRunning}
            </span>
          )}
        </button>

        <JobsPanel open={jobsOpen} onClose={() => setJobsOpen(false)} />

        <button
          type="button"
          onClick={() => setTrashOpen(true)}
          className="w-full flex items-center gap-3 px-4 py-2.5 rounded-lg text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/50 transition"
        >
          <Trash2 className="w-5 h-5" />
          <span className="font-medium text-sm">Trash</span>
        </button>

        <button
          type="button"
          onClick={() => navigate('/tags')}
          className="w-full flex items-center gap-3 px-4 py-2.5 rounded-lg text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/50 transition"
        >
          <TagsIcon className="w-5 h-5" />
          <span className="font-medium text-sm">Auto-tagging</span>
        </button>

        <button
          type="button"
          onClick={() => navigate('/duplicates')}
          className="w-full flex items-center gap-3 px-4 py-2.5 rounded-lg text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/50 transition"
        >
          <Copy className="w-5 h-5" />
          <span className="font-medium text-sm">Duplicates</span>
        </button>

        <button
          type="button"
          onClick={() => navigate('/shares')}
          className="w-full flex items-center gap-3 px-4 py-2.5 rounded-lg text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/50 transition"
        >
          <Link2 className="w-5 h-5" />
          <span className="font-medium text-sm">Client links</span>
        </button>

        <button onClick={() => navigate('/upload')} className="w-full flex items-center gap-3 px-4 py-2.5 bg-zinc-100 hover:bg-white text-zinc-950 rounded-lg font-semibold transition mt-2">
          <Upload className="w-5 h-5" />
          <span>Upload</span>
        </button>
      </nav>

      <div className="flex-1 overflow-hidden flex flex-col mt-4 px-3">
        {/* Sorting Section - Always visible in Browse view */}
        {location.pathname === '/browse' && (
          <div className="px-4 py-2 border-b border-zinc-800 mb-2">
            <p className="text-xs text-zinc-500 uppercase font-medium mb-2">Sort By</p>
            <div className="flex items-center gap-1.5 flex-wrap">
                {['date', 'size', 'duration', 'name'].map((s) => (
                  <button
                    key={s}
                    onClick={() => onSortByChange?.(s)}
                    className={cn(
                      'px-2 py-1 rounded text-xs transition',
                      sortBy === s
                        ? 'bg-zinc-700 text-zinc-100'
                        : 'text-zinc-500 hover:text-zinc-300'
                    )}
                  >
                    {s.charAt(0).toUpperCase() + s.slice(1)}
                  </button>
                ))}
                <button
                  onClick={() => onSortOrderChange?.(sortOrder === 'asc' ? 'desc' : 'asc')}
                  className="ml-1 w-7 h-7 flex items-center justify-center rounded-full bg-zinc-800 border border-zinc-700 text-zinc-400 hover:text-zinc-200 hover:border-zinc-600 transition text-xs"
                >
                  {sortOrder === 'asc' ? '↑' : '↓'}
                </button>
            </div>
          </div>
        )}

        {/* Folders - mirrors the media drive */}
        {location.pathname === '/browse' && (
          <>
            <button onClick={() => setFoldersExpanded(!foldersExpanded)} className="flex items-center justify-between px-4 py-2 text-zinc-500 hover:text-zinc-300 transition">
              <div className="flex items-center gap-2">
                <FolderTreeIcon className="w-4 h-4" />
                <span className="text-sm font-medium uppercase tracking-wider">Folders</span>
              </div>
              {foldersExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            </button>

            {foldersExpanded && (
              <>
                <label className="flex items-center gap-2 px-4 pb-2 text-xs text-zinc-500 cursor-pointer hover:text-zinc-400">
                  <input
                    type="checkbox"
                    checked={includeSubfolders}
                    onChange={(e) => setIncludeSubfolders?.(e.target.checked)}
                    className="accent-red-600"
                  />
                  Include subfolders
                </label>
                <div className="flex-1 overflow-y-auto pb-2 pr-1">
                  <FolderTree
                    tree={folderTree}
                    selectedId={selectedFolderId}
                    totalCount={stats?.total_videos ?? videos.length}
                    marked={marked}
                    onSelect={(id, e) => {
                      // Ctrl / Cmd click marks folders for merging
                      if (id != null && e && (e.ctrlKey || e.metaKey)) {
                        e.preventDefault();
                        toggleMark(id);
                        return;
                      }
                      setMarked(new Set());
                      setSelectedFolderId?.(id);
                    }}
                    onDropMedia={(folderId, mediaId) => moveVideoToFolder?.(Number(mediaId), folderId)}
                    onContextMenu={(e, node) => setFolderMenu({ x: e.clientX, y: e.clientY, node })}
                    onDropFiles={handleDropFiles}
                  />
                </div>
              </>
            )}
          </>
        )}

        {/* Tags Section - Only visible in project view */}
        {/* Tags belong to the Browse view, like Folders and Sort.
            They used to be hidden behind a FOLDER selection, which made the
            whole auto-tagging pass invisible from the default view; fixing
            that dropped the guard entirely, so they then showed up on Admin
            and Dashboard where there is nothing to filter. */}
        {location.pathname === '/browse' && (
        <>
        <button onClick={() => setTagsExpanded(!tagsExpanded)} className="flex items-center justify-between px-4 py-2 text-zinc-500 hover:text-zinc-300 transition">
          <span className="text-sm font-medium uppercase tracking-wider">
            Tags {activeTags.length > 0 && <span className="text-[#ff5c1f]">({activeTags.length})</span>}
          </span>
          {tagsExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
        </button>

        {tagsExpanded && (
          <div className="mt-1 flex-1 overflow-y-auto">
            <div className="px-3 pb-2">
              <input
                value={tagQuery}
                onChange={(e) => setTagQuery(e.target.value)}
                placeholder="Filter tags…"
                className="w-full rounded bg-zinc-900 border border-zinc-800 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-[#ff5c1f]"
              />
            </div>

            {activeTags.length > 0 && (
              <button
                onClick={() => handleTagSelect(null)}
                className="mx-3 mb-2 w-[calc(100%-1.5rem)] rounded bg-[#ff5c1f]/15 px-2 py-1 text-xs text-[#ff5c1f] hover:bg-[#ff5c1f]/25"
              >
                Clear {activeTags.length} tag{activeTags.length === 1 ? '' : 's'}
              </button>
            )}

            <div className="space-y-0.5 px-2 pb-4">
              {allTags
                .filter((t) => !tagQuery || (t.name || '').toLowerCase().includes(tagQuery.toLowerCase()))
                .map((t) => {
                  const on = activeTags.includes(t.id);
                  return (
                    <button
                      key={t.id}
                      onClick={() => handleTagSelect(t.id)}
                      className={cn(
                        "w-full flex items-center justify-between gap-2 px-2 py-1.5 rounded text-left transition",
                        on ? 'bg-[#ff5c1f]/20 text-[#ff5c1f]' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-100'
                      )}
                    >
                      <span className="truncate text-sm">{t.name}</span>
                      <span className={cn("shrink-0 font-mono text-[10px]", on ? 'text-[#ff5c1f]' : 'text-zinc-600')}>
                        {tagCounts[t.id] || 0}
                      </span>
                    </button>
                  );
                })}
              {allTags.length === 0 && (
                <p className="px-2 py-3 text-xs text-zinc-600">
                  No tags yet — open Auto-tagging and run it.
                </p>
              )}
            </div>
          </div>
        )}
        </>
        )}
      </div>

      {folderMenu && (
        <ContextMenu
          x={folderMenu.x}
          y={folderMenu.y}
          onClose={() => setFolderMenu(null)}
          items={[
            { label: 'Share with a client', icon: Link2,
              onClick: () => { setShareFolder(folderMenu.node); setFolderMenu(null); } },
            { label: 'Rename folder', icon: Edit,
              onClick: () => { setRenameFolder(folderMenu.node); setFolderMenu(null); } },
            { label: 'Delete folder', icon: Trash2, danger: true,
              onClick: () => { setDeleteFolder(folderMenu.node); setFolderMenu(null); } },
            ...(marked.size > 1 ? [{
              label: `Merge ${marked.size} folders`, icon: FolderTreeIcon,
              onClick: () => { setMergeOpen(true); setFolderMenu(null); },
            }] : []),
          ]}
        />
      )}

      <PromptDialog
        open={!!renameFolder}
        title={`Rename "${renameFolder?.name || ''}"`}
        defaultValue={renameFolder?.name || ''}
        onCancel={() => { setRenameFolder(null); setFolderError(null); }}
        onConfirm={async (value) => {
          const target = renameFolder;
          setRenameFolder(null);
          try { await dataContext?.renameProject?.(target.id, value); }
          catch (err) { setFolderError(String(err.message || err)); }
        }}
      />

      {deleteFolder && (
        <ConfirmDialog
          open={!!deleteFolder}
          title="Delete folder"
          error={folderError}
          message={`Remove "${deleteFolder.name}" from the library? Its ${deleteFolder.total_count || 0} clip(s) stay on your drive and become unfiled, unless you tick the box.`}
          extraOption={{
            checked: alsoDeleteFolderFiles,
            onChange: setAlsoDeleteFolderFiles,
            label: 'Also delete this folder and everything in it from my drive',
            hint: `Permanently removes ${deleteFolder.total_count || 0} file(s). No recycle bin — this cannot be undone.`,
          }}
          onCancel={() => { setDeleteFolder(null); setAlsoDeleteFolderFiles(false); setFolderError(null); }}
          onConfirm={async () => {
            const target = deleteFolder;
            const wipe = alsoDeleteFolderFiles;
            setDeleteFolder(null); setAlsoDeleteFolderFiles(false);
            try { await dataContext?.deleteProject?.(target.id, wipe); }
            catch (err) { setFolderError(String(err.message || err)); }
          }}
        />
      )}

      {marked.size > 0 && (
        <div className="mx-3 mb-2 p-2.5 rounded-lg bg-[#ff5c1f]/10 border border-[#ff5c1f]/40">
          <div className="text-[11px] text-orange-200 mb-2">
            {marked.size} folder{marked.size === 1 ? '' : 's'} marked
            <button type="button" onClick={() => setMarked(new Set())}
                    className="float-right text-zinc-400 hover:text-zinc-200">clear</button>
          </div>
          <button
            type="button"
            disabled={marked.size < 2}
            onClick={() => setMergeOpen(true)}
            className="w-full py-1.5 rounded bg-[#ff5c1f] hover:bg-[#ff7a45] disabled:opacity-40 disabled:hover:bg-[#ff5c1f] text-zinc-950 text-xs font-semibold transition"
          >
            Merge into one folder
          </button>
          <p className="text-[10px] text-zinc-500 mt-1.5 leading-snug">
            Ctrl+click folders to mark them. Merging moves the files on your drive.
          </p>
        </div>
      )}

      <PromptDialog
        open={mergeOpen}
        title={`Merge ${marked.size} folders — name the result`}
        defaultValue={markedNames[0] || ''}
        onCancel={() => setMergeOpen(false)}
        onConfirm={async (value) => {
          setMergeOpen(false);
          setMerging(true);
          try {
            const res = await apiCall('/api/folders/merge', {
              method: 'POST',
              body: JSON.stringify({ folder_ids: Array.from(marked), name: value }),
            });
            setMarked(new Set());
            await dataContext?.loadFolderTree?.();
            await dataContext?.loadFolders?.();
            await dataContext?.loadVideos?.();
            await dataContext?.loadStats?.();
            if (res?.folder_id) setSelectedFolderId?.(res.folder_id);
          } catch (err) {
            setFolderError(String(err.message || err));
          } finally {
            setMerging(false);
          }
        }}
      />

      {merging && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/70">
          <div className="bg-zinc-900 border border-zinc-700 rounded-xl px-6 py-5 text-sm text-zinc-200">
            Merging folders and moving files…
          </div>
        </div>
      )}

      {dropBusy && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/70">
          <div className="bg-zinc-900 border border-zinc-700 rounded-xl px-6 py-5 text-sm text-zinc-200">
            Reading files dropped on &ldquo;{dropBusy}&rdquo;&hellip;
          </div>
        </div>
      )}

      {shareFolder && (
        <ShareDialog
          folderId={shareFolder.id}
          folderName={shareFolder.name}
          onClose={() => setShareFolder(null)}
        />
      )}

      <TrashPanel
        open={trashOpen}
        onClose={() => setTrashOpen(false)}
        onChanged={() => { dataContext?.loadVideos?.(); dataContext?.loadStats?.(); }}
      />

      {pwOpen && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center">
          <div className="absolute inset-0 bg-black/70" onClick={() => setPwOpen(false)} />
          <div className="relative bg-zinc-900 border border-zinc-700 rounded-xl p-6 w-full max-w-sm mx-4 shadow-2xl">
            <h2 className="text-base font-semibold text-zinc-100 mb-1">Change password</h2>
            <p className="text-xs text-zinc-500 mb-4">
              At least 10 characters. You&rsquo;ll be signed out everywhere afterwards.
            </p>
            <input
              type="password" placeholder="Current password" value={pwCurrent}
              onChange={(e) => setPwCurrent(e.target.value)}
              className="w-full mb-2 px-3 py-2 rounded bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 outline-none focus:border-[#ff5c1f]"
            />
            <input
              type="password" placeholder="New password" value={pwNew}
              onChange={(e) => setPwNew(e.target.value)}
              className="w-full mb-3 px-3 py-2 rounded bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 outline-none focus:border-[#ff5c1f]"
            />
            {pwMsg && <p className="text-xs mb-3 text-amber-400">{pwMsg}</p>}
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => { setPwOpen(false); setPwMsg(null); }}
                      className="px-3 py-1.5 text-sm text-zinc-400 hover:text-zinc-200">Cancel</button>
              <button
                type="button"
                onClick={async () => {
                  setPwMsg(null);
                  try {
                    await apiCall('/api/me/password', {
                      method: 'POST',
                      body: JSON.stringify({ current_password: pwCurrent, new_password: pwNew }),
                    });
                    setPwCurrent(''); setPwNew(''); setPwOpen(false);
                    logout();
                  } catch (err) {
                    setPwMsg(String(err.message || err).replace(/^.*?:\s*/, ''));
                  }
                }}
                className="px-3 py-1.5 rounded bg-[#ff5c1f] hover:bg-[#ff7a45] text-zinc-950 text-sm font-semibold"
              >
                Change
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="p-4 border-t border-zinc-800">
        <button
          type="button"
          onClick={() => setPwOpen(true)}
          className="w-full flex items-center gap-3 px-4 py-2 mb-1 text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800 rounded-lg transition"
        >
          <Settings className="w-4 h-4" />
          <span className="text-sm">Change password</span>
        </button>
        <button onClick={handleLogout} className="w-full flex items-center gap-3 px-4 py-2 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 rounded-lg transition">
          <LogOut className="w-4 h-4" />
          <span className="text-sm">Sign Out</span>
        </button>
      </div>
    </aside>
  );
}
