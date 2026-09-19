import { useContext } from 'react';
import { DataContext } from '../../context/DataContext';
import { X, RotateCcw, Loader2, CheckCircle, AlertTriangle } from 'lucide-react';

export default function UploadManager() {
  const { uploadQueue, retryUpload, removeFromQueue } = useContext(DataContext);

  if (uploadQueue.length === 0) return null;

  const totalProgress = uploadQueue.length > 0 
    ? Math.round(uploadQueue.reduce((acc, t) => acc + t.progress, 0) / uploadQueue.length)
    : 0;

  return (
    <div className="fixed bottom-4 right-4 w-96 bg-zinc-900 border border-zinc-700 rounded-xl shadow-2xl overflow-hidden z-50">
      <div className="p-4 border-b border-zinc-800">
        <h3 className="font-medium text-zinc-100">Uploads ({uploadQueue.length})</h3>
        <div className="h-1.5 w-full bg-zinc-800 rounded-full mt-2 overflow-hidden">
          <div className="h-full bg-orange-500 transition-all" style={{ width: `${totalProgress}%` }} />
        </div>
      </div>
      <div className="max-h-64 overflow-y-auto p-2 space-y-2">
        {uploadQueue.map(task => (
          <div key={task.id} className="flex items-center gap-3 p-2 bg-zinc-800 rounded-lg">
            <div className="flex-1 min-w-0">
              <div className="flex justify-between items-center mb-1">
                <p className="text-xs text-zinc-300 truncate">{task.file.name}</p>
                <span className="text-[10px] text-zinc-400">{task.progress}%</span>
              </div>
              <div className="h-1 w-full bg-zinc-700 rounded-full overflow-hidden">
                <div className={`h-full transition-all ${task.status === 'error' ? 'bg-red-500' : 'bg-blue-500'}`} style={{ width: `${task.progress}%` }} />
              </div>
            </div>
            {task.status === 'error' && (
              <button onClick={() => retryUpload(task.id)} className="text-zinc-400 hover:text-orange-500"><RotateCcw className="w-4 h-4" /></button>
            )}
            <button onClick={() => removeFromQueue(task.id)} className="text-zinc-400 hover:text-red-500"><X className="w-4 h-4" /></button>
          </div>
        ))}
      </div>
    </div>
  );
}
