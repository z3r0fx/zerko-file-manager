import { useContext, useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthContext } from './context/AuthContext';
import { DataContext } from './context/DataContext';
import TopBar from './components/layout/TopBar';
import Sidebar from './components/layout/Sidebar';
import BrowsePage from './pages/BrowsePage';
import DashboardPage from './pages/DashboardPage';
import UploadPage from './pages/UploadPage';
import AdminPage from './pages/AdminPage';
import LoginPage from './pages/LoginPage';
import SetupWizard from './pages/SetupWizard';
import SharePage from './pages/SharePage';
import SharesPage from './pages/SharesPage';
import DuplicatesPage from './pages/DuplicatesPage';
import TagsPage from './pages/TagsPage';
import UploadManager from './components/shared/UploadManager';
import CustomCursor from './components/shared/CustomCursor';

export default function App() {
  return (
    <BrowserRouter>
      <AppContent />
    </BrowserRouter>
  );
}

function AppContent() {
  const { user, loading } = useContext(AuthContext);
  const [needsSetup, setNeedsSetup] = useState(false);
  const [setupChecked, setSetupChecked] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/setup/status')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled && d) setNeedsSetup(!!d.needs_setup); })
      .catch(() => { /* older build without the wizard - carry on to login */ })
      .finally(() => { if (!cancelled) setSetupChecked(true); });
    return () => { cancelled = true; };
  }, []);
  const [sortBy, setSortBy] = useState('date');
  const [sortOrder, setSortOrder] = useState('desc');

  // The custom cursor hides the real one via CSS and draws its own. That only
  // works where <CustomCursor /> is actually rendered - and it is NOT on the
  // public share page. Hooks run before that early return, so a signed-in user
  // opening their own client link got the native cursor hidden with nothing
  // drawn in its place: an invisible mouse.
  const onSharePage = typeof window !== 'undefined'
    && window.location.pathname.startsWith('/s/');

  useEffect(() => {
    if (user && !onSharePage) {
      document.body.classList.add('custom-cursor');
    } else {
      document.body.classList.remove('custom-cursor');
    }
    return () => document.body.classList.remove('custom-cursor');
  }, [user, onSharePage]);

  if (loading || !setupChecked) {
    return (
      <div className="h-screen flex items-center justify-center bg-zinc-950 text-zinc-100">
        Loading...
      </div>
    );
  }

  // A database with no accounts means nobody has set this up yet.
  if (needsSetup) {
    return <SetupWizard onComplete={() => window.location.reload()} />;
  }

  if (!user) {
    return (
      <Routes>
        {/* Public: a client link works with no account at all. */}
        <Route path="/s/:token" element={<SharePage />} />
        <Route path="*" element={<LoginPage />} />
      </Routes>
    );
  }

  // A signed-in user following their own client link should see what the
  // client sees, not get bounced into the admin UI.
  if (onSharePage) {
    return (
      <Routes>
        <Route path="/s/:token" element={<SharePage />} />
      </Routes>
    );
  }

  return (
    <div className="flex h-screen bg-zinc-950 text-zinc-100">
      <CustomCursor />
      <Sidebar sortBy={sortBy} sortOrder={sortOrder} onSortByChange={setSortBy} onSortOrderChange={setSortOrder} />
      <div className="flex-1 flex flex-col overflow-hidden">
        <TopBar />
        <main className="flex-1 overflow-hidden relative">
          <Routes>
            <Route path="/" element={<Navigate to="/browse" replace />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/browse" element={<BrowsePage sortBy={sortBy} sortOrder={sortOrder} />} />
            <Route path="/upload" element={<UploadPage />} />
            <Route path="/admin" element={<AdminPage />} />
            <Route path="/shares" element={<SharesPage />} />
            <Route path="/duplicates" element={<DuplicatesPage />} />
            <Route path="/tags" element={<TagsPage />} />
          </Routes>
        </main>
      </div>
      <UploadManager />
    </div>
  );
}
