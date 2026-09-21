/**
 * Start dragging one or more library items (toward a folder in the sidebar).
 *
 * Everything the drop needs travels in the drag itself: the whole list of ids,
 * not just the card that was grabbed. The drag image is the card's thumbnail
 * with a count badge, and the page is told a drag began so the sidebar can
 * show its Folders (the drop targets) and outline them.
 */
export function startMediaDrag(e, ids, primaryId) {
  const list = ids && ids.length ? ids : [primaryId];
  const dt = e.dataTransfer;
  dt.setData('mediaId', String(primaryId));
  dt.setData('mediaIds', JSON.stringify(list));
  dt.setData('text/plain', `${list.length} item${list.length === 1 ? '' : 's'}`);
  dt.effectAllowed = 'move';

  try {
    const src = e.currentTarget && e.currentTarget.querySelector ? e.currentTarget.querySelector('img') : null;
    const box = document.createElement('div');
    box.style.cssText = 'position:fixed;top:-500px;left:-500px;width:132px;height:80px;border-radius:10px;overflow:hidden;'
      + 'background:#27272a;border:2px solid rgb(var(--accent-rgb));box-shadow:0 10px 24px rgba(0,0,0,.5)';
    if (src && src.complete && src.naturalWidth) {
      const im = src.cloneNode();
      im.removeAttribute('class');
      im.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block';
      box.appendChild(im);
    }
    if (list.length > 1) {
      const n = document.createElement('div');
      n.textContent = String(list.length);
      n.style.cssText = 'position:absolute;right:6px;bottom:6px;min-width:24px;height:24px;padding:0 7px;border-radius:12px;'
        + 'background:rgb(var(--accent-rgb));color:rgb(var(--accent-ink));font:700 13px/24px system-ui;text-align:center';
      box.appendChild(n);
    }
    document.body.appendChild(box);
    dt.setDragImage(box, 66, 40);
    setTimeout(() => box.remove(), 0);
  } catch { /* the browser's own drag image is fine */ }

  document.body.classList.add('dragging-media');
  const done = () => {
    document.body.classList.remove('dragging-media');
    window.removeEventListener('dragend', done, true);
    window.removeEventListener('drop', done, true);
  };
  window.addEventListener('dragend', done, true);
  window.addEventListener('drop', done, true);
  window.dispatchEvent(new CustomEvent('zerko-drag-start', { detail: { ids: list } }));
}
