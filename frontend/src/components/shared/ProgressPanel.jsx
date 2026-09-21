import { useState, useEffect, useRef } from 'react';
import { X, ChevronDown, ChevronUp, Upload, CheckCircle, XCircle, Trash2 } from 'lucide-react';
import { useData } from '../../context/DataContext';

export default function ProgressPanel({ tasks = [], onClose, onUploadComplete }) {
  const { clearCompletedTasks } = useData();
  const [isMinimized, setIsMinimized] = useState(false);
  const [isVisible, setIsVisible] = useState(false);
  // A4: Encoding animation state per task id
  const [encodingTasks, setEncodingTasks] = useState({});
  // Guard to prevent duplicate encoding chains for the same task
  const processedIds = useRef(new Set());

  useEffect(() => {
    if (tasks.length > 0) setIsVisible(true);
  }, [tasks]);

  // A4: Watch for tasks hitting 100% → start encoding → loading → onUploadComplete chain
  useEffect(() => {
    const timers = [];
    tasks.forEach((task) => {
      if (task.progress === 100 && task.status === 'complete' && !processedIds.current.has(task.id)) {
        processedIds.current.add(task.id);
        // Start encoding animation
        setEncodingTasks((prev) => ({ ...prev, [task.id]: 'encoding' }));

        // After 2s → "Loading to drive..."
        const t1 = setTimeout(() => {
          setEncodingTasks((prev) => ({ ...prev, [task.id]: 'loading' }));
        }, 2000);
        timers.push(t1);

        // After 2s more → done, call onUploadComplete
        const t2 = setTimeout(() => {
          setEncodingTasks((prev) => {
            const next = { ...prev };
            delete next[task.id];
            return next;
          });
          onUploadComplete?.(task.id);
        }, 4000);
        timers.push(t2);
      }
    });
    return () => timers.forEach(clearTimeout);
  }, [tasks, onUploadComplete]);

  const activeTasks = tasks.filter((t) => t.status === 'uploading');
  const completedTasks = tasks.filter((t) => t.status === 'complete');

  const handleClose = () => {
    setIsVisible(false);
    setTimeout(onClose, 300);
  };

  if (!isVisible) return null;

  return (
    <div className="fixed bottom-4 right-4 w-96 bg-zinc-900 border border-zinc-700 rounded-lg shadow-2xl overflow-hidden transition-all z-50">
      {/* Header */}
      <div className="bg-zinc-800 border-b border-zinc-700 px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Upload className="w-4 h-4 text-blue-400" />
          <h3 className="font-semibold text-zinc-100">
            Uploads {activeTasks.length > 0 && `(${activeTasks.length})`}
          </h3>
        </div>
        <div className="flex items-center gap-2">
          {completedTasks.length > 0 && (
            <button
              onClick={clearCompletedTasks}
              className="text-zinc-400 hover:text-zinc-200 transition-colors"
              title="Clear completed"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
          <button
            onClick={() => setIsMinimized(!isMinimized)}
            className="text-zinc-400 hover:text-zinc-200 transition-colors"
          >
            {isMinimized ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </button>
          <button onClick={handleClose} className="text-zinc-400 hover:text-zinc-200 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Content */}
      {!isMinimized && (
        <div className="max-h-96 overflow-y-auto">
          {/* Active Uploads */}
          {activeTasks.map((task) => (
            <ActiveUploadCard key={task.id} task={task} />
          ))}

          {/* Completed + Encoding/Uploading chain */}
          {completedTasks.map((task) => {
            const phase = encodingTasks[task.id];
            return (
              <div key={task.id} className="px-4 py-3 border-b border-zinc-800">
                <EncodingCard filename={task.filename} phase={phase} />
              </div>
            );
          })}

          {tasks.length === 0 && (
            <div className="px-4 py-8 text-center text-zinc-500 text-sm">No uploads yet</div>
          )}
        </div>
      )}
    </div>
  );
}

// A3: Active upload card — filename, percentage bar with gradient, speed MB/s, time remaining s
function ActiveUploadCard({ task }) {
  const bps = task.bps || 0;
  const speedMB = (bps / 1024 / 1024).toFixed(2);
  const remainingBytes = (task.total || 0) - (task.loaded || 0);
  const eta = bps > 0 ? (remainingBytes / bps).toFixed(0) : '--';

  return (
    <div className="px-4 py-3 border-b border-zinc-800">
      <div className="flex items-start justify-between mb-1">
        <p className="text-sm font-medium text-zinc-200 truncate flex-1 mr-2">{task.filename}</p>
        <span className="text-xs font-medium text-blue-400 flex-shrink-0">{task.progress}%</span>
      </div>
      {/* A3: Gradient progress bar */}
      <div className="w-full bg-zinc-800 rounded-full h-1.5 overflow-hidden mb-1">
        <div
          className="h-full bg-gradient-to-r from-red-500 to-accent transition-all duration-300"
          style={{ width: `${task.progress}%` }}
        />
      </div>
      <div className="flex items-center justify-between">
        <span className="text-xs text-zinc-500">
          {speedMB} MB/s
        </span>
        <span className="text-xs text-zinc-500">{eta}s remaining</span>
      </div>
    </div>
  );
}

// A4: Encoding animation card
const DOTS = ['.', '..', '...'];
function EncodingCard({ filename, phase }) {
  const [dotIdx, setDotIdx] = useState(0);

  useEffect(() => {
    if (!phase) return;
    const id = setInterval(() => setDotIdx((i) => (i + 1) % DOTS.length), 500);
    return () => clearInterval(id);
  }, [phase]);

  return (
    <div className="flex items-center gap-2">
      <CheckCircle className="w-4 h-4 text-emerald-400 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-sm text-zinc-300 truncate">{filename}</p>
        {phase === 'encoding' && (
          <p className="text-xs text-amber-400 mt-0.5">Encoding{DOTS[dotIdx]}</p>
        )}
        {phase === 'loading' && <p className="text-xs text-blue-400 mt-0.5">Loading to drive...</p>}
      </div>
    </div>
  );
}

function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

// Provided for backwards compat — internal callbacks don't need it exposed
ProgressPanel.displayName = 'ProgressPanel';