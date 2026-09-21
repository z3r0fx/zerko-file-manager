import { useState, useEffect, useContext } from 'react';
import { apiCall } from '../lib/api';
import { HardDrive, Video, Folder, Mic, Clock } from 'lucide-react';
import MaintenancePanel from '../components/shared/MaintenancePanel';
import UpdatePanel from '../components/shared/UpdatePanel';
import StoragePanel from '../components/shared/StoragePanel';

export default function DashboardPage() {
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchData() {
      const data = await apiCall('/api/stats');
      setStats(data);
      setLoading(false);
    }
    fetchData();
  }, []);

  if (loading) return <div className="p-6 text-zinc-500">Loading dashboard...</div>;

  const handleTranscribeAll = async () => {
      const videos = await apiCall('/api/videos');
      const untranscribed = videos.filter(v => !v.transcription && v.media_type === 'video').map(v => v.id);
      if (untranscribed.length === 0) return;
      await apiCall('/api/videos/batch-transcribe', { method: 'POST', body: JSON.stringify({ video_ids: untranscribed }) });
      window.location.reload();
  };

  const jobCards = [
      { id: 'proxy', title: 'Proxy Generation', icon: Video, color: 'text-blue-400' },
      { id: 'transcription', title: 'Transcription', icon: Mic, color: 'text-emerald-400', action: handleTranscribeAll },
  ];

  return (
    // h-full + overflow-y-auto: <main> is overflow-hidden, so a page that does
    // not scroll itself simply gets cut off at the fold. It fit before the
    // storage panel was added, which is why this only showed up now.
    <div className="h-full overflow-y-auto p-6 space-y-6">
      <h1 className="text-2xl font-bold text-zinc-100">Dashboard</h1>

      <StoragePanel />

      <UpdatePanel />

      <MaintenancePanel />
      
      {/* Top Stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard title="Total Storage" value={stats.total_storage_formatted} icon={HardDrive} />
        <StatCard title="Total Assets" value={stats.total_videos} icon={Video} />
        <StatCard title="Total Projects" value={stats.total_projects} icon={Folder} />
        <StatCard title="Uploads (7d)" value={stats.uploads_week} icon={Clock} />
      </div>

      {/* Job Statuses */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {jobCards.map(job => (
            <div key={job.id} className="bg-zinc-900 p-4 rounded-lg border border-zinc-800">
                <div className="flex items-center gap-2 mb-4">
                    <job.icon className={`w-5 h-5 ${job.color}`} />
                    <h2 className="font-semibold text-zinc-200">{job.title}</h2>
                    {job.action && (
                        <button onClick={job.action} className="ml-auto text-xs bg-zinc-800 hover:bg-zinc-700 text-zinc-300 px-2 py-1 rounded transition">
                            Transcribe All
                        </button>
                    )}
                </div>
                <div className="flex gap-4">
                    {['queued', 'processing', 'completed', 'failed'].map(status => (
                        <div key={status} className="flex-1">
                            <p className="text-xs text-zinc-500 capitalize">{status}</p>
                            <p className="text-lg font-mono text-zinc-100">{stats.jobs[job.id]?.[status] || 0}</p>
                        </div>
                    ))}
                </div>
            </div>
        ))}
      </div>
    </div>
  );
}

function StatCard({ title, value, icon: Icon }) {
    return (
        <div className="bg-zinc-900 p-4 rounded-lg border border-zinc-800">
            <div className="flex items-center gap-2 mb-2">
                <Icon className="w-4 h-4 text-zinc-500" />
                <span className="text-xs text-zinc-400">{title}</span>
            </div>
            <p className="text-xl font-semibold text-zinc-100">{value}</p>
        </div>
    );
}
