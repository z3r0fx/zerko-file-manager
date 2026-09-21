import { generateId } from './utils';

const API_BASE = '';

// Request logging is off unless you ask for it: the dashboard polls every few
// seconds, and two console lines per call buried real errors. To turn it on,
// run  localStorage.setItem('zerko_debug', '1')  in the browser console.
const debugLog = (...args) => {
  try { if (localStorage.getItem('zerko_debug') === '1') console.log(...args); } catch { /* storage blocked */ }
};

async function apiCall(path, options = {}) {
  const rawToken = localStorage.getItem('token');
  // Sanitize: ensure token is not the literal string "null" or "undefined"
  const token = (rawToken && rawToken !== 'null' && rawToken !== 'undefined') ? rawToken : null;
  
  debugLog('[API] Calling:', path, 'Token present:', !!token);

  const headers = { ...options.headers };

  // Add Authorization header if token exists
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  // Add Content-Type for JSON body if body is an object or JSON string (not FormData)
  const body = options.body;
  const isJsonString = typeof body === 'string' && /^(\{|\[)/.test(body.trimStart());
  if (
    body &&
    (typeof body === 'object' || isJsonString) &&
    !(body instanceof FormData) &&
    !headers['Content-Type']
  ) {
    headers['Content-Type'] = 'application/json';
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });

  // Handle 401 - clear token but don't redirect (let caller / AuthContext handle it)
  if (response.status === 401) {
    localStorage.removeItem('token');
    const err = new Error('Unauthorized');
    err.name = 'UnauthorizedError';
    throw err;
  }

  if (!response.ok) {
    const errorText = await response.text();
    console.error('[API] Error response:', path, response.status, errorText);
    throw new Error(`${response.statusText}: ${errorText}`);
  }

  if (options.responseType === 'blob') {
    return await response.blob();
  }

  const data = await response.json();
  debugLog('[API] Success:', path, 'Data count:', Array.isArray(data) ? data.length : 'object');
  return data;
}

const CHUNK_SIZE = 10 * 1024 * 1024; // 10MB

async function chunkedUpload(file, onProgress, signal, folderId = null, relativePath = null) {
  const token = localStorage.getItem('token');
  const uploadId = generateId();
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
  const maxConcurrency = 3;
  let loaded = 0;
  let successChunks = 0;
  let lastTime = performance.now();
  let lastLoaded = 0;

  const uploadChunk = async (index) => {
    const start = index * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, file.size);
    const blob = file.slice(start, end);

    let attempts = 0;
    while (attempts < 3) {
      const formData = new FormData();
      formData.append('upload_id', uploadId);
      formData.append('chunk_index', index);
      formData.append('total_chunks', totalChunks);
      formData.append('file', blob, file.name);
      if (folderId != null) formData.append('folder_id', folderId);
      if (relativePath) formData.append('relative_path', relativePath);

      try {
        const response = await fetch(`${API_BASE}/api/upload/chunk`, {
          method: 'POST',
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          body: formData,
          signal,
        });

        if (!response.ok) {
          const errText = await response.text();
          throw new Error(`HTTP ${response.status}: ${errText}`);
        }

        loaded += (end - start);
        successChunks++;

        const now = performance.now();
        const elapsed = (now - lastTime) / 1000;
        const bps = elapsed > 0 ? (loaded - lastLoaded) / elapsed : 0;
        lastLoaded = loaded;
        lastTime = now;

        const percent = Math.round((loaded / file.size) * 100);
        onProgress(percent, loaded, file.size, bps);

        return await response.json();
      } catch (err) {
        if (signal?.aborted) throw err;
        attempts++;
        if (attempts >= 3) throw new Error(`Chunk ${index} failed after 3 retries: ${err.message}`);
        await new Promise((r) => setTimeout(r, 1000 * attempts));
      }
    }
  };

  // Upload chunks with max concurrency
  const queue = Array.from({ length: totalChunks }, (_, i) => i);
  const results = [];
  const errors = [];

  const runWorker = async () => {
    while (queue.length > 0) {
      const index = queue.shift();
      try {
        results[index] = await uploadChunk(index);
      } catch (err) {
        errors.push(err.message);
        throw err;   // uploadChunk already retried 3x
      }
    }
  };

  await Promise.all(Array(maxConcurrency).fill().map(runWorker));

  const finalResult = results.find((r) => r && r.video != null);
  if (!finalResult || !finalResult.video) {
    throw new Error(`Upload failed: ${errors.join('; ') || 'server never confirmed the file'}`);
  }
  return finalResult;
}

function apiUpload(path, formData, onProgress) {
  const token = localStorage.getItem('token');

  const headers = {};
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  // Note: NOT setting Content-Type for FormData uploads

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();

    xhr.open('POST', `${API_BASE}${path}`);

    // Set headers
    for (const [key, value] of Object.entries(headers)) {
      xhr.setRequestHeader(key, value);
    }

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        const percent = Math.round((event.loaded / event.total) * 100);
        onProgress(percent, event.loaded, event.total);
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch {
          resolve(xhr.responseText);
        }
      } else if (xhr.status === 401) {
        localStorage.removeItem('token');
        console.error('[API Upload] 401 Unauthorized');
        reject(new Error('Unauthorized - please log in again'));
      } else {
        const errorMsg = xhr.responseText || xhr.statusText || `HTTP ${xhr.status}`;
        console.error('[API Upload] Upload failed:', xhr.status, errorMsg);
        reject(new Error(`Upload failed (${xhr.status}): ${errorMsg}`));
      }
    };

    xhr.onerror = () => {
      console.error('[API Upload] Network error - XHR failed');
      console.error('[API Upload] Upload path:', `${API_BASE}${path}`);
      console.error('[API Upload] Token present:', !!token);
      reject(new Error('Network error - check connection and backend'));
    };

    xhr.send(formData);
  });
}

function getVideoStreamUrl(id) {
  return `${API_BASE}/api/video-file/${id}`;
}

export { API_BASE, apiCall, apiUpload, chunkedUpload, getVideoStreamUrl };