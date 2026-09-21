import { useState, useRef, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { ChevronDown, LogOut, User, Palette, Menu } from 'lucide-react';
import { useAuth } from '../../context/AuthContext';
import SearchBar from '../shared/SearchBar';
import AppearancePanel from '../shared/AppearancePanel';

export default function TopBar({ onMenu }) {
  const navigate = useNavigate();
  const location = useLocation();
  const onFiles = location.pathname === '/files';
  const { user, logout } = useAuth();
  const [searchValue, setSearchValue] = useState('');
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [showAppearance, setShowAppearance] = useState(false);
  const menuRef = useRef(null);

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setShowUserMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // A search belongs to the page it was typed on: leaving Library for Files
  // (or back) starts clean, and Files can clear it when you open a folder.
  useEffect(() => {
    setSearchValue('');
    // next tick: the page we are arriving at has not attached its listener yet
    const t = setTimeout(() => window.dispatchEvent(new CustomEvent('odyssey-search', { detail: '' })), 0);
    return () => clearTimeout(t);
  }, [onFiles]);
  useEffect(() => {
    const clear = () => setSearchValue('');
    window.addEventListener('zerko-search-clear', clear);
    return () => window.removeEventListener('zerko-search-clear', clear);
  }, []);

  const handleSearchChange = (value) => {
    setSearchValue(value);
    window.dispatchEvent(new CustomEvent('odyssey-search', { detail: value }));
  };

  return (
    <header className="h-16 bg-zinc-950/80 backdrop-blur border-b border-zinc-800 flex items-center justify-between gap-2 px-3 sm:px-6 flex-shrink-0 z-10">
      <div className="flex shrink-0 items-center gap-2">
        <button
          onClick={onMenu}
          aria-label="Menu"
          className="rounded-md p-2 text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100 md:hidden"
        >
          <Menu className="h-5 w-5" />
        </button>
        <h2 className="hidden text-lg font-semibold text-zinc-100 lg:block">Zerko File Manager</h2>
      </div>

      <div className="mx-0 flex min-w-0 flex-1 justify-center sm:mx-8 sm:max-w-xl">
        <SearchBar
          value={searchValue}
          onChange={handleSearchChange}
          placeholder={onFiles ? "Search files..." : "Search media..."}
          className="w-full"
        />
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => setShowAppearance(true)}
          title="Appearance"
          aria-label="Appearance"
          className="rounded-md p-2 text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100"
        >
          <Palette className="h-5 w-5" />
        </button>

        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setShowUserMenu(!showUserMenu)}
            className="flex items-center gap-2 text-zinc-400 hover:text-zinc-100 transition"
          >
            <User className="w-5 h-5" />
            <span className="text-sm font-medium">{user?.username || 'User'}</span>
            <ChevronDown className="w-4 h-4" />
          </button>

          {showUserMenu && (
            <div className="absolute right-0 mt-2 w-48 bg-zinc-900 border border-zinc-800 rounded-lg shadow-xl py-1 z-50">
              <div className="px-4 py-2 border-b border-zinc-800">
                <p className="text-sm font-medium text-zinc-100">{user?.username || 'User'}</p>
                <p className="text-xs text-zinc-500">{user?.email || ''}</p>
                {user?.role_label && (
                  <p className="mt-1 inline-block rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-zinc-400">
                    {user.role_label}
                  </p>
                )}
              </div>
              <button
                onClick={logout}
                className="w-full flex items-center gap-2 px-4 py-2 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 transition text-sm"
              >
                <LogOut className="w-4 h-4" />
                Sign Out
              </button>
            </div>
          )}
        </div>
      </div>
      {showAppearance && <AppearancePanel onClose={() => setShowAppearance(false)} />}
    </header>
  );
}
