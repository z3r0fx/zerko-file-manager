import { createContext, useState, useEffect, useContext } from 'react';
import { apiCall } from '../lib/api.js';
import { login as loginApi, logout as logoutApi, getToken } from '../lib/auth.js';

export const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const loadUser = async () => {
    try {
      const data = await apiCall('/api/me');
      setUser(data);
    } catch (error) {
      console.error('Failed to load user data:', error);
      // If loading user data fails, assume unauthenticated
      logout();
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const token = getToken();
    if (!token) {
      setLoading(false);
      return;
    }
    loadUser();
  }, []);

  const login = async (username, password) => {
    const data = await loginApi(username, password);
    // After successful login, load full user data
    await loadUser();
  };

  const logout = () => {
    logoutApi();
    setUser(null);
    window.location.href = '/';
  };

  return (
    <AuthContext.Provider value={{ user, login, logout, loading, loadUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}