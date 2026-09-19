import { useState, useMemo } from 'react';
import { useData } from '../context/DataContext.jsx';

const DEFAULT_FILTER = {
  search: '',
  tags: [],
  sortBy: 'created_at',
  sortOrder: 'desc',
  folderId: null,
  mediaType: 'all',
  collection: null,
};

export function useVideos() {
  const { videos } = useData();
  const [filter, setFilter] = useState(DEFAULT_FILTER);

  const filteredVideos = useMemo(() => {
    let result = [...videos];

    // Filter by collection
    if (filter.collection) {
      if (filter.collection === 'untranscribed') {
        result = result.filter(v => !(v.has_transcription || v.transcription) && v.media_type === 'video');
      } else if (filter.collection === 'no_proxy') {
        result = result.filter(v => !v.has_proxy && v.media_type === 'video');
      } else if (filter.collection === 'recent') {
        const oneWeekAgo = new Date();
        oneWeekAgo.setDate(oneWeekAgo.getDate() - 7);
        result = result.filter(v => new Date(v.uploaded_at || v.created_at) > oneWeekAgo);
      }
    }

    // NOTE: text search is NOT applied here any more.
    // BrowsePage owns it, because a transcript hit has to come from the server
    // (the list payload no longer carries full transcripts). Filtering here as
    // well would drop every transcript-only match before it could be merged.
    

    // Filter by tags (tag IDs)
    if (filter.tags && filter.tags.length > 0) {
      result = result.filter((v) =>
        filter.tags.some((tagId) => v.tags?.some((t) => t.id === tagId))
      );
    }

    // Filter by folder
    if (filter.folderId !== null) {
      result = result.filter((v) => v.folder_id === filter.folderId);
    }

    // Filter by mediaType
    if (filter.mediaType && filter.mediaType !== 'all') {
        result = result.filter((v) => v.media_type === filter.mediaType);
    }

    return result;
  }, [videos, filter]);

  const setSearch = (search) => setFilter((f) => ({ ...f, search }));
  const setTagFilter = (tags) => setFilter((f) => ({ ...f, tags }));
  const setSort = (sortBy, sortOrder) =>
    setFilter((f) => ({ ...f, sortBy, sortOrder }));
  const setFolderFilter = (folderId) => setFilter((f) => ({ ...f, folderId, collection: null }));
  const setMediaTypeFilter = (mediaType) => setFilter((f) => ({ ...f, mediaType }));
  const setCollectionFilter = (collection) => setFilter((f) => ({ ...f, collection, folderId: null }));

  return {
    filteredVideos,
    setFilter,
    setSearch,
    setTagFilter,
    setSort,
    setFolderFilter,
    setMediaTypeFilter,
    setCollectionFilter,
    filter,
  };
}