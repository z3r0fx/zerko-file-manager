import { X, Upload, Download, CheckCircle, AlertCircle } from 'lucide-react';

export default function ProgressPanel({ tasks, onClose }) {
  if (!tasks || tasks.length === 0) return null;

  const activeTasks = tasks.filter(t => t.status === 'uploading' || t.status === 'downloading');
  const completedTasks = tasks.filter(t => t.status === 'completed');
  const failedTasks = tasks.filter(t => t.status === 'failed');

  return (
    <div className="fixed bottom-4 right-4 w-96 bg-gray-800 border border-gray-700 rounded-lg shadow-2xl overflow-hidden z-50">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-gray-700 bg-gray-900">
        <div className="flex items-center gap-2">
          <Upload className="w-4 h-4 text-accent" />
          <h3 className="font-semibold">
            Uploads ({activeTasks.length} active)
          </h3>
        </div>
        <button
          onClick={onClose}
          className="p-1 hover:bg-gray-700 rounded transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Task List */}
      <div className="max-h-96 overflow-y-auto">
        {/* Active Tasks */}
        {activeTasks.map(task => (
          <div key={task.id} className="p-4 border-b border-gray-700">
            <div className="flex items-center gap-3 mb-2">
              <Upload className="w-4 h-4 text-accent animate-pulse" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium truncate">{task.filename}</p>
                <p className="text-xs text-gray-400">
                  {Math.round(task.progress)}% • {formatBytes(task.loaded)} / {formatBytes(task.total)}
                </p>
              </div>
            </div>
            {/* Progress Bar */}
            <div className="w-full bg-gray-700 rounded-full h-2 overflow-hidden">
              <div
                className="bg-accent h-full transition-all duration-300"
                style={{ width: `${task.progress}%` }}
              />
            </div>
          </div>
        ))}

        {/* Completed Tasks */}
        {completedTasks.map(task => (
          <div key={task.id} className="p-4 border-b border-gray-700 bg-gray-900/50">
            <div className="flex items-center gap-3">
              <CheckCircle className="w-4 h-4 text-green-500 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium truncate">{task.filename}</p>
                <p className="text-xs text-gray-400">Completed</p>
              </div>
            </div>
          </div>
        ))}

        {/* Failed Tasks */}
        {failedTasks.map(task => (
          <div key={task.id} className="p-4 border-b border-gray-700 bg-red-900/10">
            <div className="flex items-center gap-3">
              <AlertCircle className="w-4 h-4 text-red-500 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium truncate">{task.filename}</p>
                <p className="text-xs text-red-400">{task.error || 'Upload failed'}</p>
              </div>
            </div>
          </div>
        ))}

        {tasks.length === 0 && (
          <div className="p-8 text-center text-gray-500">
            <Upload className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">No active uploads</p>
          </div>
        )}
      </div>
    </div>
  );
}

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}
