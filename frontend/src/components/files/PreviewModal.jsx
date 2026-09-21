import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X, Download, ChevronLeft, ChevronRight, Share2, Loader2, ZoomIn, ZoomOut } from 'lucide-react';
import { cn } from '../../lib/utils';
import { downloadUrl, thumbUrl, filesApi, formatBytes, formatWhen, isNativeImage, canPlayVideo, canPlayAudio, startDownload } from '../../lib/files';
import { KindTile } from './FileIcon';

function TextView({ entry }) {
  const [state, setState] = useState({ loading: true });
  useEffect(() => {
    let live = true;
    setState({ loading: true });
    filesApi.text(entry.path)
      .then((r) => live && setState({ text: r.text, truncated: r.truncated }))
      .catch((e) => live && setState({ error: e.message }));
    return () => { live = false; };
  }, [entry.path]);
  if (state.loading) return <Center><Loader2 className="h-6 w-6 animate-spin text-zinc-500" /></Center>;
  if (state.error) return <Fallback entry={entry} note={state.error} />;
  const lines = state.text.split('\n');
  return (
    <div className="h-full w-full overflow-auto rounded-xl border border-zinc-800 bg-zinc-950 p-0 text-left">
      <table className="w-full table-fixed border-collapse font-mono text-[12.5px] leading-5">
        <tbody>
          {lines.map((ln, i) => (
            <tr key={i}>
              <td className="w-14 select-none border-r border-zinc-800 px-3 text-right align-top text-zinc-600">{i + 1}</td>
              <td className="whitespace-pre-wrap break-all px-4 text-zinc-200">{ln || ' '}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {state.truncated && <p className="border-t border-zinc-800 px-4 py-3 text-xs text-zinc-500">Only the start of this file is shown. Download it to see everything.</p>}
    </div>
  );
}

function Center({ children }) {
  return <div className="flex h-full w-full items-center justify-center">{children}</div>;
}

function Fallback({ entry, note }) {
  return (
    <Center>
      <div className="text-center">
        <KindTile kind={entry.kind} ext={entry.ext} className="mx-auto h-40 w-40 rounded-3xl" />
        <p className="mt-5 text-sm font-medium text-zinc-200">{entry.name}</p>
        <p className="mt-1 text-xs text-zinc-500">{note || 'There is no preview for this kind of file.'}</p>
        <button onClick={() => startDownload(downloadUrl(entry.path), entry.name)}
                className="mt-5 inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-accent-foreground hover:bg-accent-hi">
          <Download className="h-4 w-4" /> Download
        </button>
      </div>
    </Center>
  );
}

function ImageView({ entry }) {
  const [zoom, setZoom] = useState(false);
  const [failed, setFailed] = useState(false);
  const native = isNativeImage(entry);
  const src = native ? downloadUrl(entry.path, true) : thumbUrl(entry.path, 1800, entry.mtime);
  useEffect(() => { setZoom(false); setFailed(false); }, [entry.path]);
  if (failed) return <Fallback entry={entry} note="This image could not be shown in the browser." />;
  return (
    <div className={cn('relative h-full w-full', zoom ? 'overflow-auto' : 'flex items-center justify-center')}>
      <img src={src} alt={entry.name} onError={() => setFailed(true)} draggable={false}
           onClick={() => setZoom((z) => !z)}
           className={cn('select-none rounded-lg', zoom ? 'max-w-none cursor-zoom-out' : 'max-h-full max-w-full cursor-zoom-in object-contain')} />
      <button onClick={() => setZoom((z) => !z)} aria-label={zoom ? 'Fit to screen' : 'Actual size'}
              className="fixed bottom-6 right-6 rounded-full bg-zinc-900/90 p-2.5 text-zinc-300 shadow-lg ring-1 ring-zinc-700 hover:text-white">
        {zoom ? <ZoomOut className="h-4 w-4" /> : <ZoomIn className="h-4 w-4" />}
      </button>
    </div>
  );
}

function Body({ entry }) {
  const src = downloadUrl(entry.path, true);
  if (entry.kind === 'image') return <ImageView entry={entry} />;
  if (entry.kind === 'video' && canPlayVideo(entry)) {
    return <Center><video key={entry.path} src={src} controls autoPlay playsInline className="max-h-full max-w-full rounded-lg bg-black" /></Center>;
  }
  if (entry.kind === 'audio' && canPlayAudio(entry)) {
    return (
      <Center>
        <div className="w-full max-w-xl text-center">
          <KindTile kind="audio" className="mx-auto mb-6 h-44 w-44 rounded-3xl" />
          <p className="mb-4 truncate text-sm text-zinc-300">{entry.name}</p>
          <audio key={entry.path} src={src} controls autoPlay className="w-full" />
        </div>
      </Center>
    );
  }
  if (entry.kind === 'pdf') return <iframe key={entry.path} title={entry.name} src={src} className="h-full w-full rounded-xl border border-zinc-800 bg-white" />;
  if (entry.kind === 'text' || entry.kind === 'code') return <TextView entry={entry} />;
  const note = entry.kind === 'video'
    ? 'Your browser cannot play this video format. Download it to watch it.' : undefined;
  return <Fallback entry={entry} note={note} />;
}

export default function PreviewModal({ items, index, onIndex, onClose, onShare, canShare }) {
  const entry = items[index];
  const stage = useRef(null);
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.closest?.('input, textarea')) return;
      if (e.key === 'Escape') { e.stopPropagation(); onClose(); }
      else if (e.key === 'ArrowRight' && index < items.length - 1) onIndex(index + 1);
      else if (e.key === 'ArrowLeft' && index > 0) onIndex(index - 1);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [index, items.length, onIndex, onClose]);
  if (!entry) return null;

  return createPortal(
    <div className="fixed inset-0 z-[85] flex flex-col bg-zinc-950/[0.98] backdrop-blur-sm animate-shade-in" role="dialog" aria-modal="true" aria-label={`Preview of ${entry.name}`}>
      <div className="flex items-center gap-3 border-b border-zinc-800/80 px-4 py-3">
        <button onClick={onClose} aria-label="Close preview" className="rounded-lg p-2 text-zinc-400 hover:bg-zinc-800 hover:text-white"><X className="h-5 w-5" /></button>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-zinc-100">{entry.name}</p>
          <p className="truncate text-xs text-zinc-500">
            {[formatBytes(entry.size), formatWhen(entry.mtime, { long: true }), items.length > 1 && `${index + 1} of ${items.length}`].filter(Boolean).join(' - ')}
          </p>
        </div>
        {canShare && (
          <button onClick={() => onShare(entry)} className="hidden items-center gap-2 rounded-lg px-3 py-2 text-sm text-zinc-300 hover:bg-zinc-800 sm:inline-flex">
            <Share2 className="h-4 w-4" /> Share
          </button>
        )}
        <button onClick={() => startDownload(downloadUrl(entry.path), entry.name)}
                className="inline-flex items-center gap-2 rounded-lg bg-zinc-100 px-3.5 py-2 text-sm font-semibold text-zinc-950 hover:bg-white">
          <Download className="h-4 w-4" /> <span className="hidden sm:inline">Download</span>
        </button>
      </div>
      <div ref={stage} className="relative min-h-0 flex-1" onClick={(e) => { if (e.target === stage.current) onClose(); }}>
        <div className="absolute inset-0 p-4 sm:p-8"><Body entry={entry} /></div>
        {index > 0 && (
          <button onClick={() => onIndex(index - 1)} aria-label="Previous"
                  className="absolute left-3 top-1/2 -translate-y-1/2 rounded-full bg-zinc-900/90 p-3 text-zinc-300 shadow-lg ring-1 ring-zinc-700 hover:text-white">
            <ChevronLeft className="h-5 w-5" />
          </button>
        )}
        {index < items.length - 1 && (
          <button onClick={() => onIndex(index + 1)} aria-label="Next"
                  className="absolute right-3 top-1/2 -translate-y-1/2 rounded-full bg-zinc-900/90 p-3 text-zinc-300 shadow-lg ring-1 ring-zinc-700 hover:text-white">
            <ChevronRight className="h-5 w-5" />
          </button>
        )}
      </div>
    </div>,
    document.body,
  );
}
