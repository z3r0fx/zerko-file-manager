import { useState, useEffect, useCallback } from 'react';
import {
  HardDrive, Folder, ChevronRight, ChevronLeft, Check, Loader2,
  User as UserIcon, Film, AlertCircle, Home,
} from 'lucide-react';

/**
 * First-run setup.
 *
 * Shown instead of the login screen when the database has no users. Three
 * steps: who are you, where is your footage, what should run.
 *
 * The folder picker exists because a browser cannot open a native dialog for a
 * folder on the *server*. The server lists its own drives and directories and
 * the user walks the tree here.
 */
export default function SetupWizard({ onComplete }) {
  const [step, setStep] = useState(1);

  // Asked for only when the wizard is opened from another device. The code
  // is printed in the Zerko window on the machine itself.
  const [codeRequired, setCodeRequired] = useState(false);
  const [code, setCode] = useState('');

  // step 1
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');

  // step 2
  const [browse, setBrowse] = useState(null);
  const [chosen, setChosen] = useState(null);
  const [preview, setPreview] = useState(null);
  const [scanning, setScanning] = useState(false);

  // step 3
  const [proxies, setProxies] = useState(true);
  const [transcribe, setTranscribe] = useState(false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch('/api/setup/status')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d) setCodeRequired(!!d.code_required); })
      .catch(() => {});
  }, []);

  const authHeaders = useCallback(
    () => (code.trim() ? { 'X-Setup-Code': code.trim() } : {}),
    [code],
  );

  const go = useCallback(async (path) => {
    setError(null);
    try {
      const url = path ? `/api/setup/browse?path=${encodeURIComponent(path)}` : '/api/setup/browse';
      const res = await fetch(url, { headers: authHeaders() });
      if (!res.ok) throw new Error((await res.json()).detail || 'Could not read that folder');
      setBrowse(await res.json());
    } catch (e) {
      setError(e.message);
    }
  }, [authHeaders]);

  useEffect(() => { if (step === 2 && !browse) go(''); }, [step, browse, go]);

  const pick = async (path) => {
    setChosen(path);
    setPreview(null);
    setScanning(true);
    try {
      const res = await fetch(`/api/setup/scan-preview?path=${encodeURIComponent(path)}`,
                              { headers: authHeaders() });
      if (res.ok) setPreview(await res.json());
    } catch { /* preview is a nicety, not a gate */ }
    setScanning(false);
  };

  const finish = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/setup/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          username, password, media_root: chosen,
          generate_proxies: proxies, transcribe, index_now: true,
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Setup failed');
      setStep(4);
      setTimeout(() => onComplete?.(), 2500);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  const step1Valid = username.trim().length >= 2 && password.length >= 10 && password === confirm
    && (!codeRequired || code.trim().length > 0);

  return (
    <div className="min-h-screen bg-[#0B0B0D] text-zinc-100">
      <div className="mx-auto max-w-2xl px-6 py-12">
        <header className="mb-8">
          <h1 className="text-2xl font-semibold tracking-tight">
            <span className="text-[#ff5c1f]">ZERKO</span> File Manager
          </h1>
          <p className="mt-1 text-sm text-zinc-500">Let's get your library set up.</p>
        </header>

        <Steps current={step} />

        <div className="mt-8 rounded-xl border border-zinc-800 bg-zinc-900/50 p-6">
          {step === 1 && (
            <>
              <StepHead icon={UserIcon} title="Create your account"
                        hint="This is the only account, and it's the admin. Nothing is sent anywhere — it lives on this machine." />
              {codeRequired && (
                <Field label="Setup code"
                       hint="You're opening this from another device. The code is shown in the Zerko window on the PC that runs it.">
                  <input value={code} onChange={(e) => setCode(e.target.value)}
                         autoFocus className={input} placeholder="from the Zerko window" />
                </Field>
              )}
              <Field label="Username">
                <input value={username} onChange={(e) => setUsername(e.target.value)}
                       autoFocus={!codeRequired} className={input} placeholder="your name" />
              </Field>
              <Field label="Password" hint="At least 10 characters. Three unrelated words works well.">
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                       className={input} />
              </Field>
              <Field label="Confirm password">
                <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)}
                       className={input} />
              </Field>
              {password && password.length < 10 && (
                <Hint bad>{10 - password.length} more character{10 - password.length === 1 ? '' : 's'} needed</Hint>
              )}
              {confirm && password !== confirm && <Hint bad>Passwords don't match</Hint>}
            </>
          )}

          {step === 2 && (
            <>
              <StepHead icon={Folder} title="Where do you keep your footage?"
                        hint="Pick the top folder. Everything inside it, including subfolders, gets catalogued. Your files are never moved or changed." />

              {browse?.parent !== undefined && browse?.path && (
                <div className="mb-3 flex items-center gap-2 text-xs">
                  <button onClick={() => go('')} className="flex items-center gap-1 text-zinc-400 hover:text-zinc-100">
                    <Home className="h-3.5 w-3.5" /> Drives
                  </button>
                  {browse.parent && (
                    <button onClick={() => go(browse.parent)} className="flex items-center gap-1 text-zinc-400 hover:text-zinc-100">
                      <ChevronLeft className="h-3.5 w-3.5" /> Up
                    </button>
                  )}
                  <span className="truncate font-mono text-zinc-500">{browse.path}</span>
                </div>
              )}

              <div className="max-h-64 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950">
                {browse?.drives?.map((d) => (
                  <button key={d.path} onClick={() => go(d.path)}
                          className="flex w-full items-center gap-3 border-b border-zinc-800/60 px-4 py-3 text-left transition last:border-0 hover:bg-zinc-800/50">
                    <HardDrive className="h-4 w-4 shrink-0 text-[#ff5c1f]" />
                    <span className="flex-1 font-mono text-sm">{d.label}</span>
                    <span className="font-mono text-xs text-zinc-500">{d.free_formatted} free</span>
                    <ChevronRight className="h-4 w-4 text-zinc-600" />
                  </button>
                ))}

                {browse?.folders?.map((f) => (
                  <div key={f.path}
                       className={'flex items-center gap-3 border-b border-zinc-800/60 px-4 py-2.5 last:border-0 ' +
                         (chosen === f.path ? 'bg-[#ff5c1f]/10' : 'hover:bg-zinc-800/40')}>
                    <button onClick={() => go(f.path)} className="flex min-w-0 flex-1 items-center gap-3 text-left">
                      <Folder className="h-4 w-4 shrink-0 text-zinc-500" />
                      <span className="truncate text-sm">{f.name}</span>
                    </button>
                    <button onClick={() => pick(f.path)}
                            className={'shrink-0 rounded px-2.5 py-1 text-xs transition ' +
                              (chosen === f.path
                                ? 'bg-[#ff5c1f] text-black'
                                : 'border border-zinc-700 text-zinc-400 hover:text-zinc-100')}>
                      {chosen === f.path ? 'Selected' : 'Use this'}
                    </button>
                  </div>
                ))}

                {browse && !browse.drives && browse.folders?.length === 0 && (
                  <p className="px-4 py-6 text-center text-sm text-zinc-600">No subfolders here.</p>
                )}
              </div>

              {browse?.path && !browse.drives && (
                <button onClick={() => pick(browse.path)}
                        className={'mt-3 w-full rounded-lg border px-4 py-2 text-sm transition ' +
                          (chosen === browse.path
                            ? 'border-[#ff5c1f] bg-[#ff5c1f]/10 text-[#ff5c1f]'
                            : 'border-zinc-700 text-zinc-300 hover:bg-zinc-800')}>
                  {chosen === browse.path ? 'Selected: ' : 'Use this folder: '}
                  <span className="font-mono text-xs">{browse.path}</span>
                </button>
              )}

              {(scanning || preview) && (
                <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/60 px-4 py-3">
                  {scanning ? (
                    <p className="flex items-center gap-2 text-sm text-zinc-400">
                      <Loader2 className="h-4 w-4 animate-spin" /> Looking through that folder…
                    </p>
                  ) : preview.files === 0 ? (
                    <p className="flex items-center gap-2 text-sm text-amber-400">
                      <AlertCircle className="h-4 w-4" /> No media found in there — is that the right folder?
                    </p>
                  ) : (
                    <p className="text-sm text-zinc-300">
                      Found <strong className="font-mono text-[#ff5c1f]">{preview.files.toLocaleString()}</strong>
                      {preview.truncated ? '+' : ''} files · {preview.total_formatted}
                      <span className="mt-1 block font-mono text-xs text-zinc-500">
                        {preview.kinds.video} video · {preview.kinds.photo} photo · {preview.kinds.audio} audio
                      </span>
                    </p>
                  )}
                </div>
              )}
            </>
          )}

          {step === 3 && (
            <>
              <StepHead icon={Film} title="What should run after indexing?"
                        hint="Both can be turned on later. Neither changes your original files." />
              <Opt checked={proxies} onChange={setProxies} title="Generate proxies"
                   detail="Small streaming copies, about 1% of your library size. Without them, anyone watching from outside your network downloads the full-size original." />
              <Opt checked={transcribe} onChange={setTranscribe} title="Transcribe speech"
                   detail="Makes everything searchable by what's said, and tags clips automatically. Downloads about 2GB of AI models and wants a decent graphics card — leave off if unsure."
                   warn={transcribe} />
            </>
          )}

          {step === 4 && (
            <div className="py-6 text-center">
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-emerald-600/20">
                <Check className="h-6 w-6 text-emerald-400" />
              </div>
              <h2 className="text-lg font-medium">You're set up</h2>
              <p className="mt-1 text-sm text-zinc-400">
                Indexing has started in the background. Signing you in…
              </p>
            </div>
          )}

          {error && (
            <p className="mt-4 flex items-center gap-2 text-sm text-red-400">
              <AlertCircle className="h-4 w-4 shrink-0" /> {error}
            </p>
          )}

          {step < 4 && (
            <div className="mt-6 flex items-center justify-between">
              <button onClick={() => setStep((s) => s - 1)} disabled={step === 1}
                      className="text-sm text-zinc-500 transition hover:text-zinc-200 disabled:invisible">
                Back
              </button>
              {step < 3 ? (
                <button
                  onClick={() => setStep((s) => s + 1)}
                  disabled={(step === 1 && !step1Valid) || (step === 2 && !chosen)}
                  className="rounded-lg bg-[#ff5c1f] px-5 py-2 text-sm font-medium text-black transition hover:bg-[#ff7a45] disabled:opacity-30"
                >
                  Continue
                </button>
              ) : (
                <button onClick={finish} disabled={busy}
                        className="flex items-center gap-2 rounded-lg bg-[#ff5c1f] px-5 py-2 text-sm font-medium text-black transition hover:bg-[#ff7a45] disabled:opacity-50">
                  {busy && <Loader2 className="h-4 w-4 animate-spin" />}
                  {busy ? 'Setting up…' : 'Finish setup'}
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

const input =
  'w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-[#ff5c1f]';

function Steps({ current }) {
  const labels = ['Account', 'Media folder', 'Options'];
  return (
    <div className="flex items-center gap-2">
      {labels.map((label, i) => {
        const n = i + 1;
        const done = current > n;
        const active = current === n;
        return (
          <div key={label} className="flex flex-1 items-center gap-2">
            <div className={
              'flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-medium ' +
              (done ? 'bg-emerald-600 text-white'
                : active ? 'bg-[#ff5c1f] text-black' : 'bg-zinc-800 text-zinc-500')
            }>
              {done ? <Check className="h-3.5 w-3.5" /> : n}
            </div>
            <span className={'text-xs ' + (active ? 'text-zinc-200' : 'text-zinc-600')}>{label}</span>
            {n < labels.length && <div className="h-px flex-1 bg-zinc-800" />}
          </div>
        );
      })}
    </div>
  );
}

function StepHead({ icon: Icon, title, hint }) {
  return (
    <div className="mb-5">
      <h2 className="flex items-center gap-2 text-base font-medium text-zinc-100">
        <Icon className="h-4 w-4 text-[#ff5c1f]" /> {title}
      </h2>
      {hint && <p className="mt-1.5 text-sm leading-relaxed text-zinc-500">{hint}</p>}
    </div>
  );
}

function Field({ label, hint, children }) {
  return (
    <label className="mb-3 block">
      <span className="mb-1 block text-xs text-zinc-500">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-zinc-600">{hint}</span>}
    </label>
  );
}

function Hint({ children, bad }) {
  return <p className={'text-xs ' + (bad ? 'text-amber-500' : 'text-zinc-500')}>{children}</p>;
}

function Opt({ checked, onChange, title, detail, warn }) {
  return (
    <label className={
      'mb-3 flex cursor-pointer gap-3 rounded-lg border p-3 transition ' +
      (checked ? 'border-[#ff5c1f]/50 bg-[#ff5c1f]/5' : 'border-zinc-800 hover:border-zinc-700')
    }>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)}
             className="mt-0.5 h-4 w-4 accent-[#ff5c1f]" />
      <span>
        <span className="block text-sm font-medium text-zinc-200">{title}</span>
        <span className="mt-0.5 block text-xs leading-relaxed text-zinc-500">{detail}</span>
        {warn && (
          <span className="mt-1.5 flex items-center gap-1 text-xs text-amber-500">
            <AlertCircle className="h-3 w-3" /> First run will download ~2GB before anything is transcribed.
          </span>
        )}
      </span>
    </label>
  );
}
