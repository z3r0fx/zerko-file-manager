import { useState, useEffect, useCallback } from 'react';
import { apiCall } from '../lib/api.js';
import { useData } from '../context/DataContext.jsx';

export function useStats() {
  const { stats, loadStats } = useData();
  const [serverStats, setServerStats] = useState(null);

  const refresh = useCallback(async () => {
    await loadStats();
    const ss = await apiCall('/api/server-stats');
    setServerStats(ss);
    return ss;
  }, [loadStats]);

  useEffect(() => {
    // Initial fetch
    refresh();

    // Poll every 5 seconds
    const interval = setInterval(refresh, 5000);

    return () => clearInterval(interval);
  }, [refresh]);

  return { stats, serverStats, refresh };
}