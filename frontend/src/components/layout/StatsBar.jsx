import { useContext } from 'react';
import { Play, HardDrive, Mic, CheckCircle } from 'lucide-react';
import { DataContext } from '../../context/DataContext';

export default function StatsBar() {
  const { videos, stats } = useContext(DataContext);

  // Compute derived stats from videos
  const transcribedCount = videos.filter((v) => v.has_transcription || v.transcription).length;
  const deliveredCount = videos.filter((v) => v.status === 'delivered').length;

  const statCards = [
    {
      id: 'videos',
      label: 'Total Files',
      value: stats?.total_videos ?? videos.length ?? 0,
      icon: Play,
      color: 'text-blue-400',
    },
    {
      id: 'storage',
      label: 'Storage Used',
      value: stats?.total_storage_formatted ?? '—',
      icon: HardDrive,
      color: 'text-amber-400',
    },
    {
      id: 'transcribed',
      label: 'Transcribed',
      value: transcribedCount,
      icon: Mic,
      color: 'text-purple-400',
    },
    {
      id: 'delivered',
      label: 'Delivered',
      value: deliveredCount,
      icon: CheckCircle,
      color: 'text-green-400',
    },
  ];

  return (
    <div className="flex gap-4 overflow-x-auto pb-2">
      {statCards.map((stat) => {
        const Icon = stat.icon;
        const isNumeric = typeof stat.value === 'number';

        return (
          <div
            key={stat.id}
            className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-4 flex items-center gap-4 min-w-48 flex-shrink-0"
          >
            <div className={`p-2.5 rounded-lg bg-zinc-800 ${stat.color}`}>
              <Icon className="w-5 h-5" />
            </div>
            <div>
              <p className="text-xl font-semibold text-zinc-100">
                {isNumeric ? stat.value.toLocaleString() : stat.value}
              </p>
              <p className="text-sm text-zinc-500">{stat.label}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}