import { useState, useRef, useEffect } from 'react';
import { Play, Pause, Repeat, Repeat1 } from 'lucide-react';
import { cn } from '../../lib/utils';
export default function AudioPlayer({ audio, volume = 0.5, onTogglePlay }) {
  const [isPlaying, setIsPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const [loop, setLoop] = useState(false);
  const audioRef = useRef(new Audio(`/api/video-file/${audio.id}?token=${localStorage.getItem('token')}`));

  useEffect(() => {
    audioRef.current.volume = volume;
    audioRef.current.loop = loop;
  }, [volume, loop]);

  useEffect(() => {
    const audioEl = audioRef.current;
    const updateProgress = () => setProgress((audioEl.currentTime / audioEl.duration) * 100);
    audioEl.addEventListener('timeupdate', updateProgress);
    audioEl.addEventListener('ended', () => { if (!loop) setIsPlaying(false); });

    if (onTogglePlay) onTogglePlay(togglePlay);

    return () => {
      audioEl.removeEventListener('timeupdate', updateProgress);
      audioEl.pause();
    };
  }, [loop]);

  const togglePlay = () => {
    if (isPlaying) audioRef.current.pause();
    else audioRef.current.play();
    setIsPlaying(!isPlaying);
  };

  return (
    <div className="flex flex-col gap-2 p-3 bg-zinc-800 rounded-lg">
      <div className="flex items-center gap-2">
        <button onClick={(e) => { e.stopPropagation(); togglePlay(); }} className="p-2 bg-red-600 text-white rounded-full">
            {isPlaying ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
        </button>
        <button onClick={(e) => { e.stopPropagation(); setLoop(!loop); }} className={cn("p-2 rounded-full", loop ? "text-red-500" : "text-zinc-400")}>
            {loop ? <Repeat1 className="w-4 h-4" /> : <Repeat className="w-4 h-4" />}
        </button>
        <span className="text-xs text-zinc-100 flex-1 truncate">{audio.filename}</span>
        <span className="text-xs text-zinc-300">{audio.duration_formatted}</span>
      </div>
      <div className="flex gap-0.5 h-8 items-end">
        {[...Array(20)].map((_, i) => (
            <div key={i} className="flex-1 bg-zinc-700" style={{ height: `${20 + Math.random() * 60}%` }} />
        ))}
      </div>
    </div>
  );
}
