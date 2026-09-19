/**
 * The catalog stores everything in one `videos` table, but the library is
 * photos, audio and documents as well. These helpers pick the right noun so
 * the UI never tells you it is about to "delete 6 video(s)" when you have six
 * stills selected.
 */

const NOUNS = {
  video: ['video', 'videos'],
  photo: ['photo', 'photos'],
  audio: ['audio file', 'audio files'],
  document: ['document', 'documents'],
};

const FALLBACK = ['file', 'files'];

/** Noun for a single item, e.g. "photo". */
export function nounFor(media) {
  const pair = NOUNS[media?.media_type] || FALLBACK;
  return pair[0];
}

/**
 * Noun for a set of items, pluralised by `count`. When the selection mixes
 * types it falls back to the neutral "file"/"files".
 */
export function nounForMany(items, count) {
  const n = typeof count === 'number' ? count : (items?.length ?? 0);
  const types = new Set((items || []).map((m) => m?.media_type).filter(Boolean));
  const pair = types.size === 1 ? (NOUNS[[...types][0]] || FALLBACK) : FALLBACK;
  return n === 1 ? pair[0] : pair[1];
}

/** "6 photos", "1 video", "4 files". */
export function countLabel(items, count) {
  const n = typeof count === 'number' ? count : (items?.length ?? 0);
  return `${n} ${nounForMany(items, n)}`;
}

/** Title Case for dialog headings: "Delete Photo". */
export function titleNoun(media) {
  return nounFor(media).replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Transcription only makes sense for things that can carry audio. */
export function canTranscribe(media) {
  return media?.media_type === 'video' || media?.media_type === 'audio';
}
