import { API_BASE, chunkedUpload } from './api';

// 10MB chunks, 3 in parallel, 3 retries each, and resumable - which matters a
// lot when a single clip can be 10GB. The old implementation sent one giant
// unresumable POST: lose the connection at 95% and it started over.
const CHUNK_THRESHOLD = 10 * 1024 * 1024;

function uploadWholeFile(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE}${path}`);
    const token = localStorage.getItem('token');
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 95));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        try { resolve(JSON.parse(xhr.response)); } catch { resolve(xhr.response); }
      } else {
        reject(new Error(xhr.statusText || `Upload failed (${xhr.status})`));
      }
    };
    xhr.onerror = () => reject(new Error('Upload failed - check the connection'));
    xhr.send(formData);
  });
}

export async function uploadFile(file, onProgress, onStatusChange, folderId = null, signal = null, relativePath = null) {
  onStatusChange?.('uploading');
  try {
    let result;
    if (file.size > CHUNK_THRESHOLD) {
      result = await chunkedUpload(
        file,
        (percent) => onProgress?.(percent),
        signal,
        folderId,
        relativePath
      );
    } else {
      const formData = new FormData();
      formData.append('file', file);
      if (folderId != null) formData.append('folder_id', folderId);
      // Uploading a directory: the server rebuilds this structure underneath
      // the folder you're in, instead of flattening everything into one place.
      if (relativePath) formData.append('relative_path', relativePath);
      result = await uploadWholeFile('/api/upload', formData, onProgress);
    }
    onStatusChange?.('done');
    return { success: true, result };
  } catch (err) {
    onStatusChange?.('error');
    throw err;
  }
}
