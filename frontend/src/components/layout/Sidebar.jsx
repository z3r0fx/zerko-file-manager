import { useState, useContext, useEffect, useRef, useMemo } from 'react';
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
import { CAP } from '../../lib/capabilities';
import { useAppearance, DEFAULTS, SIDEBAR_MIN, SIDEBAR_MAX } from '../../context/AppearanceContext';
import { useNavigate, useLocation } from 'react-router-dom';
import { X, Library, LayoutDashboard, Upload, LogOut, ChevronDown, ChevronRight, FolderOpen, FolderTree as FolderTreeIcon, Activity, Edit, Trash2, Link2, Copy, Tags as TagsIcon, SlidersHorizontal, KeyRound, Users } from 'lucide-react';
import { AuthContext } from '../../context/AuthContext';
import { DataContext } from '../../context/DataContext';
import { useVideos } from '../../hooks/useVideos';
import { setSidebarSlot } from '../../lib/sidebarSlot';
import { useBrowsingType } from '../../lib/mediaTypeStore';

export default function Sidebar({ sortBy, sortOrder, onSortByChange, onSortOrderChange,
                                  mobileOpen = false, onMobileClose = () => {} }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout, can } = useContext(AuthContext);
  const dataContext = useContext(DataContext);
  const { filter } = useVideos();
  
  const videos = dataContext?.videos || [];
  const fullTree = dataContext?.folderTree || [];
  const selectedFolderId = dataContext?.selectedFolderId ?? null;

  // Photos / Videos / Audio tab: show only the folders that hold that kind of
  // media (plus the folder you are standing in, and the way down to it, so the
  // tree never pulls the floor out from under you). Counts follow the tab too.
  const mediaTab = useBrowsingType();
  const typedTree = useMemo(() => {
    if (!['video', 'photo', 'audio'].includes(mediaTab)) return null;
    const keepSelected = (node) => {
      if (node.id === selectedFolderId) return true;
      return (node.children || []).some(keepSelected);
    };
    const prune = (nodes) => (nodes || []).flatMap((n) => {
      if (!n.total_by_type) return [n];            // older backend: leave the tree alone
      const sub = n.total_by_type[mediaTab] || 0;
      if (sub <= 0 && !keepSelected(n)) return [];
      return [{ ...n, total_count: sub, video_count: (n.by_type || {})[mediaTab] || 0, children: prune(n.children) }];
    });
    return prune(fullTree);
  }, [fullTree, mediaTab, selectedFolderId]);
  const folderTree = typedTree || fullTree;
  const typedRootTotal = typedTree && fullTree.every((n) => n.total_by_type)
    ? typedTree.reduce((a, n) => a + (n.total_count || 0), 0) : null;
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

  // One panel at a time: Folders or Tags. They used to be stacked, which is
  // what made the sidebar feel crowded.
  const [panel, setPanel] = useState(() => { try { return localStorage.getItem('zerko.sidebar.panel') === 'tags' ? 'tags' : 'folders'; } catch { return 'folders'; } });
  const choosePanel = (p) => { setPanel(p); try { localStorage.setItem('zerko.sidebar.panel', p); } catch { /* private mode */ } };
  useEffect(() => {
    const show = () => setPanel('folders');
    window.addEventListener('zerko-drag-start', show);
    return () => window.removeEventListener('zerko-drag-start', show);
  }, []);
  const [manageOpen, setManageOpen] = useState(() => { try { return localStorage.getItem('zerko.sidebar.manage') === '1'; } catch { return false; } });
  const toggleManage = () => setManageOpen((o) => { try { localStorage.setItem('zerko.sidebar.manage', o ? '0' : '1'); } catch { /* ignore */ } return !o; });
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
  })(fullTree);
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

  const handleNav = (path) => { navigate(path); onMobileClose(); };

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
    <aside
      style={{ width: 'var(--sidebar-w)' }}
      className={cn(
        // Below md this is a drawer sliding over the content; from md up it is
        // an ordinary column again and the transform is dropped entirely.
        "fixed inset-y-0 left-0 z-50 flex h-[100dvh] max-w-[85vw] flex-col",
        "border-r border-zinc-800 bg-zinc-950 transition-transform duration-200",
        // transform-none matters: ANY transform makes this a containing block for
        // position:fixed descendants, which would trap the jobs panel inside the
        // sidebar's stacking context and let the page paint over it.
        "md:relative md:z-auto md:max-w-none md:flex-shrink-0 md:transform-none md:transition-none",
        mobileOpen ? "translate-x-0 shadow-2xl" : "-translate-x-full",
      )}
    >
      <button
        onClick={onMobileClose}
        aria-label="Close menu"
        className="absolute right-2 top-2 z-10 rounded-md p-2 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 md:hidden"
      >
        <X className="h-5 w-5" />
      </button>
      <SidebarResizer />
      <div className="px-5 pb-3 pt-5">
        <h1 className="flex items-center gap-2.5 select-none">
          <span
            aria-hidden="true"
            className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-accent shadow-lg shadow-accent/20"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="rgb(var(--accent-ink))" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
              <rect x="2" y="5" width="14" height="14" rx="2" />
              <path d="M16 10l6-3v10l-6-3" />
            </svg>
          </span>
          <span className="flex flex-col leading-none">
            <span className="text-[17px] font-bold tracking-tight text-accent">
              ZERKO
            </span>
            <span className="text-[10px] font-medium uppercase tracking-[0.22em] text-zinc-300 mt-0.5">
              File Manager
            </span>
          </span>
        </h1>
      </div>

      {can(CAP.UPLOAD) && currentView !== 'files' && (
        <div className="px-4 pb-3">
          <button
            onClick={() => handleNav('/upload')}
            className="flex w-full items-center justify-center gap-2 rounded-xl bg-accent py-2 text-sm font-semibold text-accent-foreground shadow-md shadow-black/30 transition hover:bg-accent-hi"
          >
            <Upload className="h-4 w-4" /> Upload
          </button>
        </div>
      )}

      <nav className="space-y-0.5 px-3 pt-0.5">
        <NavItem icon={Library} label="Library" active={currentView === 'browse'} onClick={() => handleNav('/browse')} />
        <NavItem icon={FolderOpen} label="Files" active={currentView === 'files'} onClick={() => handleNav('/files')} />
        {can(CAP.SHARES) && (
          <NavItem icon={Link2} label="Portals" active={currentView === 'shares'} onClick={() => handleNav('/shares')} />
        )}

        {/* Everything that is not part of the day-to-day lives in one place. */}
        <div className="pt-2">
          <button
            type="button"
            onClick={toggleManage}
            aria-expanded={manageOpen}
            className={cn(
              'flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-sm transition',
              jobsRunning > 0 ? 'job-pulse border-zinc-700 text-zinc-100' : 'border-transparent text-zinc-500 hover:bg-zinc-800/50 hover:text-zinc-200',
            )}
          >
            <span className="relative grid h-5 w-5 place-items-center">
              <SlidersHorizontal className="h-[18px] w-[18px]" />
              {jobsRunning > 0 && <span className="job-dot-pulse absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-accent" />}
            </span>
            <span className="flex-1 text-left font-medium">Manage</span>
            {jobsRunning > 0 && !manageOpen && (
              <span className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[11px] text-zinc-200">{jobsRunning}</span>
            )}
            {manageOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </button>

          {manageOpen && (
            <div className="ml-4 mt-0.5 space-y-0.5 border-l border-zinc-800 pl-2">
              <button
                type="button"
                onClick={() => setJobsOpen((v) => !v)}
                aria-expanded={jobsOpen}
                title={jobsRunning > 0 ? `${jobsRunning} job${jobsRunning === 1 ? '' : 's'} in progress` : 'No jobs running - click to view'}
                className={cn('flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm transition',
                  jobsOpen ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-100')}
              >
                <span className="relative grid h-4 w-4 place-items-center">
                  <Activity className="h-4 w-4" />
                  {jobsRunning > 0 && <span className="job-dot-pulse absolute -right-1 -top-1 h-2 w-2 rounded-full bg-accent" />}
                </span>
                <span className="flex-1 text-left">Jobs</span>
                {jobsRunning > 0 && <span className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[11px] text-zinc-200">{jobsRunning}</span>}
              </button>
              {can(CAP.TRASH) && <SubItem icon={Trash2} label="Trash" onClick={() => setTrashOpen(true)} />}
              {can(CAP.ADMIN) && <SubItem icon={Copy} label="Duplicates" active={currentView === 'duplicates'} onClick={() => handleNav('/duplicates')} />}
              {can(CAP.ADMIN) && <SubItem icon={TagsIcon} label="Auto-tagging" active={currentView === 'tags'} onClick={() => handleNav('/tags')} />}
              {can(CAP.ADMIN) && <SubItem icon={LayoutDashboard} label="Dashboard" active={currentView === 'dashboard'} onClick={() => handleNav('/dashboard')} />}
              {(can(CAP.ADMIN) || can(CAP.CREATE_CLIENTS)) && (
                <SubItem icon={Users} label={can(CAP.ADMIN) ? 'Users' : 'Client accounts'} active={currentView === 'admin'} onClick={() => handleNav('/admin')} />
              )}
            </div>
          )}
        </div>
        <JobsPanel open={jobsOpen} onClose={() => setJobsOpen(false)} />
      </nav>

      {/* Other pages (Files) put their own navigation here. */}
      <div ref={currentView === 'files' ? setSidebarSlot : undefined} className={cn('min-h-0 flex-1 flex-col', currentView === 'files' ? 'mt-3 flex' : 'hidden')} />

      <div className={cn('mt-3 min-h-0 flex-1 flex-col px-3', currentView === 'files' ? 'hidden' : 'flex')}>
        {/* Folders and Tags only mean something in the Library. */}
        {location.pathname === '/browse' && (
          <>
            <div className="mb-2 flex rounded-lg border border-zinc-800 bg-zinc-900/60 p-0.5 text-xs" role="tablist">
              {[['folders', 'Folders', FolderTreeIcon, 0], ['tags', 'Tags', TagsIcon, activeTags.length]].map(([id, label, Icon, n]) => (
                <button
                  key={id}
                  role="tab"
                  aria-selected={panel === id}
                  onClick={() => choosePanel(id)}
                  className={cn('flex flex-1 items-center justify-center gap-1.5 rounded-md py-1.5 font-medium transition',
                    panel === id ? 'bg-zinc-700 text-zinc-50' : 'text-zinc-500 hover:text-zinc-300')}
                >
                  <Icon className="h-3.5 w-3.5" /> {label}
                  {n > 0 && <span className="rounded-full bg-accent px-1.5 text-[10px] font-semibold leading-4 text-accent-foreground">{n}</span>}
                </button>
              ))}
            </div>

            {panel === 'folders' && (
              <>
                {activeTags.length > 0 && (
                  <button
                    onClick={() => handleTagSelect(null)}
                    className="mb-2 flex w-full items-center justify-between rounded-md bg-accent/15 px-2.5 py-1.5 text-xs text-accent hover:bg-accent/25"
                  >
                    <span>Filtering by {activeTags.length} tag{activeTags.length === 1 ? '' : 's'}</span>
                    <span className="text-[11px] opacity-80">Clear</span>
                  </button>
                )}
                <label className="flex cursor-pointer items-center gap-2 px-2 pb-2 text-xs text-zinc-500 hover:text-zinc-400">
                  <input
                    type="checkbox"
                    checked={includeSubfolders}
                    onChange={(e) => setIncludeSubfolders?.(e.target.checked)}
                    className="accent-red-600"
                  />
                  Include subfolders
                </label>
                <div className="min-h-0 flex-1 overflow-y-auto pb-2 pr-1">
                  <FolderTree
                    tree={folderTree}
                    selectedId={selectedFolderId}
                    totalCount={typedRootTotal ?? stats?.total_videos ?? videos.length}
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
                    onDropMedia={(folderId, mediaId, mediaIds) => {
                      // the whole selection travels with the drag, not just the clip you grabbed
                      const ids = (mediaIds && mediaIds.length ? mediaIds : [mediaId]).map(Number).filter(Boolean);
                      dataContext?.moveVideosToFolder?.(ids, folderId);
                    }}
                    onContextMenu={(e, node) => setFolderMenu({ x: e.clientX, y: e.clientY, node })}
                    onDropFiles={handleDropFiles}
                  />
                </div>
              </>
            )}

            {panel === 'tags' && (
              <div className="flex min-h-0 flex-1 flex-col">
                <input
                  value={tagQuery}
                  onChange={(e) => setTagQuery(e.target.value)}
                  placeholder="Filter tags..."
                  className="mb-2 w-full rounded-md border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-accent"
                />
                {activeTags.length > 0 && (
                  <button
                    onClick={() => handleTagSelect(null)}
                    className="mb-2 w-full rounded-md bg-accent/15 px-2 py-1.5 text-xs text-accent hover:bg-accent/25"
                  >
                    Clear {activeTags.length} tag{activeTags.length === 1 ? '' : 's'}
                  </button>
                )}
                <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto pb-3">
                  {allTags
                    .filter((t) => !tagQuery || (t.name || '').toLowerCase().includes(tagQuery.toLowerCase()))
                    .map((t) => {
                      const on = activeTags.includes(t.id);
                      return (
                        <button
                          key={t.id}
                          onClick={() => handleTagSelect(t.id)}
                          className={cn(
                            'flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left transition',
                            on ? 'bg-accent/20 text-accent' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-100',
                          )}
                        >
                          <span className="truncate text-sm">{t.name}</span>
                          <span className={cn('shrink-0 font-mono text-[10px]', on ? 'text-accent' : 'text-zinc-600')}>
                            {tagCounts[t.id] || 0}
                          </span>
                        </button>
                      );
                    })}
                  {allTags.length === 0 && (
                    <p className="px-2 py-3 text-xs text-zinc-600">No tags yet - open Auto-tagging and run it.</p>
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
            { label: 'Create a portal', icon: Link2,
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
        <div className="mx-3 mb-2 p-2.5 rounded-lg bg-accent/10 border border-accent/40">
          <div className="text-[11px] text-accent-hi mb-2">
            {marked.size} folder{marked.size === 1 ? '' : 's'} marked
            <button type="button" onClick={() => setMarked(new Set())}
                    className="float-right text-zinc-400 hover:text-zinc-200">clear</button>
          </div>
          <button
            type="button"
            disabled={marked.size < 2}
            onClick={() => setMergeOpen(true)}
            className="w-full py-1.5 rounded bg-accent hover:bg-accent-hi disabled:opacity-40 disabled:hover:bg-accent text-zinc-950 text-xs font-semibold transition"
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
              className="w-full mb-2 px-3 py-2 rounded bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 outline-none focus:border-accent"
            />
            <input
              type="password" placeholder="New password" value={pwNew}
              onChange={(e) => setPwNew(e.target.value)}
              className="w-full mb-3 px-3 py-2 rounded bg-zinc-800 border border-zinc-700 text-sm text-zinc-100 outline-none focus:border-accent"
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
                className="px-3 py-1.5 rounded bg-accent hover:bg-accent-hi text-zinc-950 text-sm font-semibold"
              >
                Change
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="border-t border-zinc-800 px-4 py-3">
        <div className={cn('mb-2.5 items-baseline justify-between text-[11px]', currentView === 'files' ? 'hidden' : 'flex')}>
          <span className="text-zinc-500">Library</span>
          <span className="text-zinc-400">
            {stats?.total_storage_formatted ?? '0 B'} <span className="text-zinc-600">- {(stats?.total_videos ?? videos.length ?? 0).toLocaleString()} files</span>
          </span>
        </div>
        <div className="flex items-center gap-1">
          <span className="min-w-0 flex-1 truncate px-1 text-sm text-zinc-300" title={user?.username}>{user?.username}</span>
          <button
            type="button"
            onClick={() => setPwOpen(true)}
            title="Change password"
            aria-label="Change password"
            className="rounded-lg p-2 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200"
          >
            <KeyRound className="h-4 w-4" />
          </button>
          <button
            onClick={handleLogout}
            title="Sign out"
            aria-label="Sign out"
            className="rounded-lg p-2 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </aside>
  );
}

function NavItem({ icon: Icon, label, active, onClick }) {
  return (
    <button
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      className={cn('flex w-full items-center gap-3 rounded-lg px-3 py-2 text-[15px] transition',
        active ? 'bg-zinc-800 text-zinc-50' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-100')}
    >
      <Icon className={cn('h-5 w-5', active && 'text-accent')} />
      <span className="font-medium">{label}</span>
    </button>
  );
}

function SubItem({ icon: Icon, label, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className={cn('flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm transition',
        active ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-100')}
    >
      <Icon className="h-4 w-4" />
      <span>{label}</span>
    </button>
  );
}


/**
 * The draggable edge of the sidebar.
 *
 * Grab the line between the sidebar and the page and drag. The width goes
 * straight onto the document root while you drag and is saved on release -
 * dragging through app-wide state re-renders the whole tree on every mouse
 * move, which stutters badly on a folder tree this size. It snaps to the
 * default width when you are near it, double-click resets, and the arrow keys
 * nudge it when the edge has focus.
 */
function SidebarResizer() {
  const { sidebarWidth, set } = useAppearance();
  const [drag, setDrag] = useState(null);          // { w, y } while dragging
  const live = useRef(null);

  const clampW = (w) => Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, w));
  const snap = (w) => (Math.abs(w - DEFAULTS.sidebarWidth) <= 8 ? DEFAULTS.sidebarWidth : w);
  const apply = (w) => document.documentElement.style.setProperty('--sidebar-w', `${w}px`);

  const onPointerDown = (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture?.(e.pointerId);
    live.current = sidebarWidth;
    document.body.classList.add('select-none');
    document.body.style.cursor = 'col-resize';
    setDrag({ w: sidebarWidth, y: e.clientY });
  };
  const onPointerMove = (e) => {
    if (live.current == null) return;
    const left = e.currentTarget.parentElement.getBoundingClientRect().left;
    const w = snap(clampW(Math.round(e.clientX - left)));
    live.current = w;
    apply(w);
    setDrag({ w, y: e.clientY });
  };
  const finish = () => {
    if (live.current == null) return;
    const w = live.current;
    live.current = null;
    document.body.classList.remove('select-none');
    document.body.style.cursor = '';
    setDrag(null);
    if (w !== sidebarWidth) set({ sidebarWidth: w });
  };
  const onKeyDown = (e) => {
    const step = e.shiftKey ? 48 : 16;
    if (e.key === 'ArrowLeft') { e.preventDefault(); set({ sidebarWidth: clampW(sidebarWidth - step) }); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); set({ sidebarWidth: clampW(sidebarWidth + step) }); }
    else if (e.key === 'Home' || e.key === 'Enter') { e.preventDefault(); set({ sidebarWidth: DEFAULTS.sidebarWidth }); }
  };

  return (
    <>
      <div
        role="separator"
        aria-label="Resize sidebar"
        aria-orientation="vertical"
        aria-valuemin={SIDEBAR_MIN}
        aria-valuemax={SIDEBAR_MAX}
        aria-valuenow={sidebarWidth}
        tabIndex={0}
        title="Drag to resize - double-click to reset"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={finish}
        onPointerCancel={finish}
        onLostPointerCapture={finish}
        onKeyDown={onKeyDown}
        onDoubleClick={() => set({ sidebarWidth: DEFAULTS.sidebarWidth })}
        className="group absolute right-0 top-0 z-30 h-full w-3 translate-x-1/2 cursor-col-resize touch-none outline-none max-md:hidden"
        data-cursor="hot"
      >
        <div className={cn('absolute left-1/2 top-0 h-full -translate-x-1/2 transition-all duration-150',
          drag ? 'w-0.5 bg-accent' : 'w-px bg-transparent group-hover:w-0.5 group-hover:bg-accent/60 group-focus-visible:w-0.5 group-focus-visible:bg-accent/60')} />
        <div className={cn('absolute left-1/2 top-1/2 h-12 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full transition-all duration-150',
          drag ? 'bg-accent opacity-100' : 'bg-zinc-500 opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100')} />
      </div>
      {drag && (
        <div className="pointer-events-none fixed z-[9999] rounded-full bg-zinc-100 px-2.5 py-1 font-mono text-[11px] font-medium text-zinc-900 shadow-lg"
             style={{ left: drag.w + 18, top: Math.max(12, drag.y - 14) }}>
          {drag.w}px{drag.w === DEFAULTS.sidebarWidth ? ' - default' : ''}
        </div>
      )}
    </>
  );
}
