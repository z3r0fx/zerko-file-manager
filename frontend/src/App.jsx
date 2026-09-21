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
import FilesPage from './pages/FilesPage';
import FileSharePage from './pages/FileSharePage';
import UploadTray from './components/files/UploadTray';
import SharesPage from './pages/SharesPage';
import DuplicatesPage from './pages/DuplicatesPage';
import TagsPage from './pages/TagsPage';
import UploadManager from './components/shared/UploadManager';
import CustomCursor from './components/shared/CustomCursor';
import { useAppearance } from './context/AppearanceContext';
import { CAP } from './lib/capabilities';

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
  const { cursor } = useAppearance();
  const { can } = useContext(AuthContext);
  const [sortBy, setSortBy] = useState('date');
  const [sortOrder, setSortOrder] = useState('desc');
  // Below md the sidebar is a drawer rather than a column.
  const [navOpen, setNavOpen] = useState(false);

  // The custom cursor hides the real one via CSS and draws its own. That only
  // works where <CustomCursor /> is actually rendered - and it is NOT on the
  // public share page. Hooks run before that early return, so a signed-in user
  // opening their own client link got the native cursor hidden with nothing
  // drawn in its place: an invisible mouse.
  const onSharePage = typeof window !== 'undefined'
    && (window.location.pathname.startsWith('/s/') || window.location.pathname.startsWith('/f/'));

  useEffect(() => {
    // 'system' means we draw nothing, so the native cursor must NOT be hidden.
    if (user && !onSharePage && (cursor === 'dot' || cursor === 'ring')) {
      document.body.classList.add('custom-cursor');
    } else {
      document.body.classList.remove('custom-cursor');
    }
    return () => document.body.classList.remove('custom-cursor');
  }, [user, onSharePage, cursor]);

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
        <Route path="/f/:token" element={<FileSharePage />} />
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
        <Route path="/f/:token" element={<FileSharePage />} />
      </Routes>
    );
  }

  return (
    <div className="flex h-[100dvh] bg-zinc-950 text-zinc-100">
      <CustomCursor />
      {navOpen && (
        <div
          className="animate-shade-in fixed inset-0 z-40 bg-black/60 md:hidden"
          onClick={() => setNavOpen(false)}
        />
      )}
      <Sidebar
        sortBy={sortBy}
        sortOrder={sortOrder}
        onSortByChange={setSortBy}
        onSortOrderChange={setSortOrder}
        mobileOpen={navOpen}
        onMobileClose={() => setNavOpen(false)}
      />
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <TopBar onMenu={() => setNavOpen(true)} />
        <main className="flex-1 overflow-hidden relative">
          <Routes>
            <Route path="/" element={<Navigate to="/browse" replace />} />
            {/* Typing a URL should not get you a page your role cannot use.
                The server refuses the data either way; this is so the answer
                is a sentence rather than a screen of failed requests. */}
            <Route path="/browse" element={<BrowsePage sortBy={sortBy} sortOrder={sortOrder} onSortByChange={setSortBy} onSortOrderChange={setSortOrder} />} />
            <Route path="/files" element={<Gated ok={can(CAP.READ)}><FilesPage /></Gated>} />
            <Route path="/dashboard" element={<Gated ok={can(CAP.ADMIN)}><DashboardPage /></Gated>} />
            <Route path="/upload" element={<Gated ok={can(CAP.UPLOAD)}><UploadPage /></Gated>} />
            <Route path="/admin" element={<Gated ok={can(CAP.ADMIN) || can(CAP.CREATE_CLIENTS)}><AdminPage /></Gated>} />
            <Route path="/shares" element={<Gated ok={can(CAP.SHARES)}><SharesPage /></Gated>} />
            <Route path="/duplicates" element={<Gated ok={can(CAP.ADMIN)}><DuplicatesPage /></Gated>} />
            <Route path="/tags" element={<Gated ok={can(CAP.ADMIN)}><TagsPage /></Gated>} />
          </Routes>
        </main>
      </div>
      <UploadManager />
      <UploadTray />
    </div>
  );
}

function Gated({ ok, children }) {
  if (ok) return children;
  return (
    <div className="flex h-full items-center justify-center p-8 text-center">
      <div>
        <p className="text-sm font-medium text-zinc-300">That page is not available on your account.</p>
        <p className="mt-1 text-xs text-zinc-500">Ask an administrator if you think it should be.</p>
      </div>
    </div>
  );
}
