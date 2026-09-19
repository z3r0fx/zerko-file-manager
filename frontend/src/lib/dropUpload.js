/**
 * Turn a drag-and-drop DataTransfer into a flat list of files, keeping the
 * folder structure of anything that was a directory.
 *
 * dataTransfer.files only ever contains top-level entries - dropping a folder
 * gives you the folder itself and none of its contents. webkitGetAsEntry() is
 * the only way to walk into it, and the entries are neutered as soon as the
 * drop handler yields, so they MUST be grabbed synchronously first.
 */

function readEntries(reader) {
  return new Promise((resolve) => {
    reader.readEntries((batch) => resolve(batch), () => resolve([]));
  });
}

async function walkEntry(entry, prefix, out) {
  if (!entry) return;

  if (entry.isFile) {
    const file = await new Promise((resolve) => entry.file(resolve, () => resolve(null)));
    if (file && file.size > 0) {
      out.push({ file, relativePath: prefix ? `${prefix}/${file.name}` : null });
    }
    return;
  }

  if (entry.isDirectory) {
    const dirPath = prefix ? `${prefix}/${entry.name}` : entry.name;
    const reader = entry.createReader();
    // readEntries returns at most ~100 at a time, so keep asking until empty
    let batch = await readEntries(reader);
    while (batch.length) {
      for (const child of batch) await walkEntry(child, dirPath, out);
      batch = await readEntries(reader);
    }
  }
}

export function hasFiles(dataTransfer) {
  if (!dataTransfer) return false;
  const types = Array.from(dataTransfer.types || []);
  return types.includes('Files');
}

export async function collectDroppedFiles(dataTransfer) {
  const out = [];
  const items = dataTransfer?.items;

  // Snapshot the entries synchronously - awaiting first loses them
  let entries = null;
  if (items && items.length && typeof items[0].webkitGetAsEntry === 'function') {
    entries = [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].kind !== 'file') continue;
      const e = items[i].webkitGetAsEntry();
      if (e) entries.push(e);
    }
  }

  if (entries && entries.length) {
    for (const entry of entries) await walkEntry(entry, '', out);
    if (out.length) return out;
  }

  // Older browsers, or a drop with no entry API: loose files only
  for (const file of Array.from(dataTransfer?.files || [])) {
    if (file.size > 0) out.push({ file, relativePath: null });
  }
  return out;
}
