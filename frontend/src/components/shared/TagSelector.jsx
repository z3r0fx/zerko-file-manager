import { useState, useContext } from 'react';
import { X, Plus, Check, X as XIcon } from 'lucide-react';
import { DataContext } from '../../context/DataContext';
import TagTree from './TagTree';
import { cn } from '../../lib/utils';

export default function TagSelector({ video, onClose }) {
  const { tagTree, tags, updateVideoTags, removeVideoTag, createTag, deleteTag, loadVideos, loadTags } = useContext(DataContext);
  const [newTagName, setNewTagName] = useState('');
  const [newTagParentId, setNewTagParentId] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [expandedTags, setExpandedTags] = useState(new Set());

  // Local mutable copy of tags for instant UI updates
  const [localTagIds, setLocalTagIds] = useState(() =>
    video?.tags?.map((t) => t.id) || []
  );

  const handleToggleTag = async (tagId) => {
    const isActive = localTagIds.includes(tagId);
    const newTagIds = isActive
      ? localTagIds.filter((id) => id !== tagId)
      : [...localTagIds, tagId];

    // Optimistic update - update UI immediately
    setLocalTagIds(newTagIds);

    // Persist to server
    try {
      if (isActive) {
        await removeVideoTag(video.id, tagId);
      } else {
        await updateVideoTags(video.id, newTagIds);
      }
      await loadVideos();
    } catch (err) {
      // Revert on failure
      setLocalTagIds(localTagIds);
      console.error('Failed to update tag:', err);
    }
  };

  const handleCreateAndAdd = async () => {
    if (!newTagName.trim()) return;
    setIsCreating(true);
    try {
      const parentId = newTagParentId ? parseInt(newTagParentId, 10) : null;
      const newTag = await createTag(newTagName.trim(), parentId);
      setLocalTagIds((prev) => [...prev, newTag.id]);
      await updateVideoTags(video.id, [...localTagIds, newTag.id]);
      await loadVideos();
      setNewTagName('');
      setNewTagParentId('');
    } catch (err) {
      console.error('Failed to create tag:', err);
    } finally {
      setIsCreating(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') {
      handleCreateAndAdd();
    }
  };

  const handleDeleteTag = async (tagId) => {
    try {
      await deleteTag(tagId);
      await loadVideos();
      await loadTags();
      setLocalTagIds((prev) => prev.filter((id) => id !== tagId));
    } catch (err) {
      console.error('Failed to delete tag:', err);
    }
  };

  // Build a map of tag id to tag for quick lookup
  const tagMap = {};
  tags.forEach(tag => { tagMap[tag.id] = tag; });

  // Helper to collect all tag IDs from tree (including children)
  const collectAllTagIds = (treeNodes) => {
    const ids = [];
    const collect = (nodes) => {
      nodes.forEach(node => {
        ids.push(node.id);
        if (node.children && node.children.length > 0) {
          collect(node.children);
        }
      });
    };
    collect(treeNodes);
    return ids;
  };

  const allTagIds = collectAllTagIds(tagTree);

  // Find root tags (tags without parent_id) for the dropdown
  const rootTags = tags.filter(tag => !tag.parent_id);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div className="relative bg-zinc-900 border border-zinc-700 rounded-xl p-6 max-w-md w-full mx-4 shadow-2xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-zinc-100">Manage Tags</h2>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-100 transition"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="mb-6 max-h-60 overflow-y-auto">
          {tagTree.length === 0 ? (
            <p className="text-zinc-500 text-sm">No tags available. Create one below.</p>
          ) : (
            <TagTree
              tags={tagTree}
              selectedTagId={null}
              onSelect={handleToggleTag}
              selectedTagIds={localTagIds}
              onDelete={handleDeleteTag}
              showActions={true}
            />
          )}
        </div>

        <div className="space-y-3">
          <div className="flex gap-2">
            <input
              type="text"
              value={newTagName}
              onChange={(e) => setNewTagName(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="New tag name..."
              className="flex-1 bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-red-500 transition"
            />
            <button
              onClick={handleCreateAndAdd}
              disabled={!newTagName.trim() || isCreating}
              className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-500 transition disabled:opacity-50 disabled:cursor-not-allowed font-medium"
            >
              {isCreating ? '...' : <Plus className="w-4 h-4" />}
            </button>
          </div>
          
          {/* Parent tag dropdown */}
          <select
            value={newTagParentId}
            onChange={(e) => setNewTagParentId(e.target.value)}
            className="w-full bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-zinc-300 focus:outline-none focus:border-red-500 transition cursor-pointer text-sm"
          >
            <option value="">No parent (root level)</option>
            {rootTags.map((tag) => (
              <option key={tag.id} value={tag.id}>
                Parent: {tag.name}
              </option>
            ))}
          </select>
        </div>
      </div>
    </div>
  );
}