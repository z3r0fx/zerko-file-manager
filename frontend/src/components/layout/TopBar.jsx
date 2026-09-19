import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronDown, LogOut, User } from 'lucide-react';
import { useAuth } from '../../context/AuthContext';
import SearchBar from '../shared/SearchBar';

export default function TopBar() {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const [searchValue, setSearchValue] = useState('');
  const [showUserMenu, setShowUserMenu] = useState(false);
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

  const handleSearchChange = (value) => {
    setSearchValue(value);
    window.dispatchEvent(new CustomEvent('odyssey-search', { detail: value }));
  };

  return (
    <header className="h-16 bg-zinc-950/80 backdrop-blur border-b border-zinc-800 flex items-center justify-between px-6 flex-shrink-0 z-10">
      <div className="flex items-center gap-6">
        <h2 className="text-lg font-semibold text-zinc-100">Zerko File Manager</h2>
      </div>

      <div className="flex-1 flex justify-center max-w-xl mx-8">
        <SearchBar
          value={searchValue}
          onChange={handleSearchChange}
          placeholder="Search media..."
          className="w-full"
        />
      </div>

      <div className="flex items-center gap-4">
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
    </header>
  );
}
