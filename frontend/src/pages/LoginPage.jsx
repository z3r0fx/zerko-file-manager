import { useState } from 'react';

const field =
  'w-full bg-zinc-800 border border-zinc-700 rounded-lg px-4 py-2.5 text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition-colors';

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { res, data };
}

export default function LoginPage() {
  const [mode, setMode] = useState('login'); // 'login' | 'recover'
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(false);

  const handleLogin = async (e) => {
    e.preventDefault();
    setError('');
    setNotice('');
    setLoading(true);
    try {
      const { res, data } = await post('/api/login', { username, password });
      if (!res.ok) {
        // Say so when the server is refusing because of too many tries -
        // otherwise a locked-out person keeps retyping a correct password.
        setError(res.status === 429
          ? (data.detail || 'Too many attempts. Wait a few minutes and try again.')
          : 'Invalid username or password');
        return;
      }
      localStorage.setItem('token', data.token);
      window.location.reload();
    } catch {
      setError('Could not reach the server. Is the Zerko window still open?');
    } finally {
      setLoading(false);
    }
  };

  const handleRecover = async (e) => {
    e.preventDefault();
    setError('');
    if (password.length < 10) return setError('Use at least 10 characters.');
    if (password !== confirm) return setError("The passwords don't match.");
    setLoading(true);
    try {
      const { res, data } = await post('/api/recover', {
        username, code: code.trim(), new_password: password,
      });
      if (!res.ok) {
        setError(data.detail || 'Could not reset the password.');
        return;
      }
      setPassword('');
      setConfirm('');
      setCode('');
      setMode('login');
      setNotice('Password changed. Sign in with the new one.');
    } catch {
      setError('Could not reach the server. Is the Zerko window still open?');
    } finally {
      setLoading(false);
    }
  };

  const recovering = mode === 'recover';

  return (
    <div className="min-h-screen bg-zinc-950 flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-8 shadow-2xl">
          <h1 className="text-2xl font-bold text-zinc-100 mb-2 text-center">Zerko File Manager</h1>
          <p className="text-sm text-zinc-400 text-center mb-8">
            {recovering ? 'Reset a forgotten password' : 'Sign in to your account'}
          </p>

          <form onSubmit={recovering ? handleRecover : handleLogin} className="space-y-4">
            {error && (
              <div className="bg-red-900/30 border border-red-700 text-red-300 rounded px-3 py-2 text-sm">
                {error}
              </div>
            )}
            {notice && (
              <div className="bg-emerald-900/30 border border-emerald-700 text-emerald-300 rounded px-3 py-2 text-sm">
                {notice}
              </div>
            )}

            {recovering && (
              <p className="text-xs leading-relaxed text-zinc-500">
                The recovery code is printed in the Zerko window on the PC that runs the server
                (the black window). Whoever can see that window can reset a password.
              </p>
            )}

            <div>
              <label htmlFor="username" className="block text-sm text-zinc-400 mb-1">Username</label>
              <input id="username" type="text" value={username}
                     onChange={(e) => setUsername(e.target.value)} required
                     autoComplete="username" className={field} placeholder="your username" />
            </div>

            {recovering && (
              <div>
                <label htmlFor="code" className="block text-sm text-zinc-400 mb-1">Recovery code</label>
                <input id="code" type="text" value={code}
                       onChange={(e) => setCode(e.target.value)} required
                       autoComplete="off" className={field} placeholder="from the Zerko window" />
              </div>
            )}

            <div>
              <label htmlFor="password" className="block text-sm text-zinc-400 mb-1">
                {recovering ? 'New password' : 'Password'}
              </label>
              <input id="password" type="password" value={password}
                     onChange={(e) => setPassword(e.target.value)} required
                     autoComplete={recovering ? 'new-password' : 'current-password'}
                     className={field} placeholder="••••••••" />
            </div>

            {recovering && (
              <div>
                <label htmlFor="confirm" className="block text-sm text-zinc-400 mb-1">Confirm new password</label>
                <input id="confirm" type="password" value={confirm}
                       onChange={(e) => setConfirm(e.target.value)} required
                       autoComplete="new-password" className={field} placeholder="••••••••" />
              </div>
            )}

            <button type="submit" disabled={loading}
                    className="w-full bg-accent hover:bg-accent-hi disabled:opacity-60 text-accent-foreground font-semibold rounded-lg px-4 py-2.5 transition-colors">
              {loading ? 'Working...' : recovering ? 'Reset password' : 'Login'}
            </button>

            <button type="button"
                    onClick={() => { setMode(recovering ? 'login' : 'recover'); setError(''); setNotice(''); setPassword(''); }}
                    className="w-full text-center text-sm text-zinc-500 hover:text-zinc-300 transition-colors">
              {recovering ? 'Back to sign in' : 'Forgot password?'}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
