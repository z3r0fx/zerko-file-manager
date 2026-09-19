import { useState, useEffect, useContext } from 'react';
import { useNavigate } from 'react-router-dom';
import { AuthContext } from '../context/AuthContext';
import { DataContext } from '../context/DataContext';
import { apiCall } from '../lib/api';
import ConfirmDialog from '../components/shared/ConfirmDialog';

export default function AdminPage() {
  const { user } = useContext(AuthContext);
  const { stats, loadStats } = useContext(DataContext);
  const navigate = useNavigate();

  const [users, setUsers] = useState([]);
  const [usersUsage, setUsersUsage] = useState([]);
  const [deleteUserTarget, setDeleteUserTarget] = useState(null);

  const [newUser, setNewUser] = useState({ username: '', email: '', password: '', role: 'user' });
  const [createUserError, setCreateUserError] = useState('');

  useEffect(() => {
    if (user?.role !== 'admin') return;
    loadUsers();
    loadStats();
    loadUsersUsage();
  }, [user]);

  const loadUsers = async () => {
    try {
      const data = await apiCall('/api/users');
      setUsers(data);
    } catch (err) {
      console.error('Failed to load users', err);
    }
  };

  const loadUsersUsage = async () => {
    try {
      const data = await apiCall('/api/admin/users-usage');
      setUsersUsage(data);
    } catch (err) {
      console.error('Failed to load users usage', err);
    }
  };

  const handleForceLogout = async (userId) => {
    try {
      await apiCall(`/api/admin/users/${userId}/logout`, { method: 'POST' });
      loadUsers();
    } catch (err) {
      console.error('Failed to force logout user', err);
    }
  };

  const handleDeleteUser = async () => {
    if (!deleteUserTarget) return;
    try {
      await apiCall(`/api/users/${deleteUserTarget.id}`, { method: 'DELETE' });
      loadUsers();
    } catch (err) {
      console.error('Failed to delete user', err);
    }
    setDeleteUserTarget(null);
  };

  const handleCreateUser = async (e) => {
    e.preventDefault();
    setCreateUserError('');
    try {
      await apiCall('/api/register', {
        method: 'POST',
        body: JSON.stringify(newUser),
      });
      setNewUser({ username: '', email: '', password: '', role: 'user' });
      loadUsers();
    } catch (err) {
      setCreateUserError(err.message || 'Failed to create user');
      console.error('Failed to create user', err);
    }
  };

  if (user?.role !== 'admin') {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="bg-red-900/20 border border-red-700 text-red-300 rounded-lg p-8 text-center">
          <p className="text-xl font-semibold mb-2">Access Denied</p>
          <p className="text-sm text-red-400">You do not have permission to view this page.</p>
          <button
            onClick={() => navigate('/browse')}
            className="mt-4 px-4 py-2 bg-red-700 hover:bg-red-600 rounded text-sm transition-colors"
          >
            Return to Browse
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full">
<div className="flex-1 space-y-8 p-6 h-full overflow-y-auto">
      {/* Stats Overview */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6">
          <p className="text-sm text-zinc-400 mb-1">Total Files</p>
          <p className="text-3xl font-bold text-zinc-100">{stats?.total_videos ?? 0}</p>
        </div>
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6">
          <p className="text-sm text-zinc-400 mb-1">Total Storage</p>
          <p className="text-3xl font-bold text-zinc-100">{stats?.total_storage_formatted ?? '0 B'}</p>
        </div>
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6">
          <p className="text-sm text-zinc-400 mb-1">Active Users</p>
          <p className="text-3xl font-bold text-zinc-100">{users.filter((u) => u.is_active).length}</p>
        </div>
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6">
          <p className="text-sm text-zinc-400 mb-1">Total Users</p>
          <p className="text-3xl font-bold text-zinc-100">{users.length}</p>
        </div>
      </div>

      {/* User Management */}
      <section>
        <h2 className="text-xl font-semibold text-zinc-100 mb-4">User Management</h2>

        {/* Create Account Form */}
        <form onSubmit={handleCreateUser} className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 mb-4">
          <h3 className="text-sm font-medium text-zinc-300 mb-3">Create Account</h3>
          <div className="flex gap-3 items-end">
            <div className="flex-1">
              <label className="block text-xs text-zinc-500 mb-1">Username</label>
              <input
                type="text"
                value={newUser.username}
                onChange={(e) => setNewUser((u) => ({ ...u, username: e.target.value }))}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-500"
                placeholder="username"
                required
              />
            </div>
            <div className="flex-1">
              <label className="block text-xs text-zinc-500 mb-1">Email</label>
              <input
                type="email"
                value={newUser.email}
                onChange={(e) => setNewUser((u) => ({ ...u, email: e.target.value }))}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-500"
                placeholder="email@example.com"
                required
              />
            </div>
            <div className="flex-1">
              <label className="block text-xs text-zinc-500 mb-1">Password</label>
              <input
                type="password"
                value={newUser.password}
                onChange={(e) => setNewUser((u) => ({ ...u, password: e.target.value }))}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-500"
                placeholder="password"
                required
              />
            </div>
            <div className="flex-1">
              <label className="block text-xs text-zinc-500 mb-1">Role</label>
              <select
                value={newUser.role}
                onChange={(e) => setNewUser((u) => ({ ...u, role: e.target.value }))}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500"
              >
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>
            </div>
            <button
              type="submit"
              className="bg-red-600 hover:bg-red-500 text-white rounded-lg px-4 py-2 font-medium transition-colors"
            >
              Create Account
            </button>
          </div>
          {createUserError && (
            <p className="mt-2 text-sm text-red-400">{createUserError}</p>
          )}
        </form>

        {/* User Table */}
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-zinc-400 text-left">
                <th className="px-4 py-3 font-medium">Username</th>
                <th className="px-4 py-3 font-medium">Email</th>
                <th className="px-4 py-3 font-medium">Role</th>
                <th className="px-4 py-3 font-medium">Created</th>
                <th className="px-4 py-3 font-medium">Last Login</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-b border-zinc-800 last:border-0 hover:bg-zinc-800/50">
                  <td className="px-4 py-3 text-zinc-100">{u.username}</td>
                  <td className="px-4 py-3 text-zinc-400">{u.email}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-medium ${
                        u.role === 'admin'
                          ? 'bg-purple-900/40 text-purple-300'
                          : 'bg-zinc-700 text-zinc-300'
                      }`}
                    >
                      {u.role}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-zinc-400">{u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}</td>
                  <td className="px-4 py-3 text-zinc-400">{u.last_login ? new Date(u.last_login).toLocaleString() : '—'}</td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${u.is_active ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'}`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${u.is_active ? 'bg-green-400' : 'bg-red-400'}`} />
                      {u.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="px-4 py-3 flex gap-2">
                    {u.is_active && u.id !== user.id && (
                      <button
                        onClick={() => handleForceLogout(u.id)}
                        className="text-orange-400 hover:text-orange-300 transition-colors text-xs"
                      >
                        Force Logout
                      </button>
                    )}
                    <button
                      onClick={(e) => { e.stopPropagation(); setDeleteUserTarget(u); }}
                      className="text-red-400 hover:text-red-300 transition-colors text-xs"
                      disabled={u.id === user.id}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Per-Account Usage */}
      <section>
        <h2 className="text-xl font-semibold text-zinc-100 mb-4">Per-Account Usage</h2>
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-zinc-400 text-left">
                <th className="px-4 py-3 font-medium">Username</th>
                <th className="px-4 py-3 font-medium">Files Uploaded</th>
                <th className="px-4 py-3 font-medium">Storage Used</th>
              </tr>
            </thead>
            <tbody>
              {usersUsage.length === 0 ? (
                <tr>
                  <td colSpan={3} className="px-4 py-6 text-center text-zinc-500">No usage data available.</td>
                </tr>
              ) : (
                usersUsage.map((u) => (
                  <tr key={u.username} className="border-b border-zinc-800 last:border-0 hover:bg-zinc-800/50">
                    <td className="px-4 py-3 text-zinc-100">{u.username}</td>
                    <td className="px-4 py-3 text-zinc-300">{u.video_count}</td>
                    <td className="px-4 py-3 text-zinc-300">{u.total_size_formatted}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Inline Delete Confirmation Modal */}
      {deleteUserTarget && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[9999]">
          <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 w-96 shadow-2xl">
            <h3 className="text-lg font-semibold text-zinc-100 mb-2">Delete User</h3>
            <p className="text-sm text-zinc-400 mb-6">
              Are you sure you want to delete user "{deleteUserTarget.username}"?
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setDeleteUserTarget(null)}
                className="px-4 py-2 text-sm font-medium text-zinc-300 hover:text-zinc-100"
              >
                Cancel
              </button>
              <button
                onClick={handleDeleteUser}
                className="px-4 py-2 text-sm font-medium bg-red-600 text-white rounded hover:bg-red-500"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
      </div>
    </div>
  );
}