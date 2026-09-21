import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevronRight, ChevronDown, Check, X, Folder, HardDrive, Loader2, AlertTriangle } from 'lucide-react';
import { cn } from '../../lib/utils';
import { filesApi, baseName, joinPath } from '../../lib/files';

// ------------------------------------------------------------------ shell
export function Modal({ open, onClose, title, children, footer, width = 'max-w-md', busy = false }) {
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === 'Escape' && !busy) { e.stopPropagation(); onClose(); } };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [open, onClose, busy]);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-[80] flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-[2px]" onClick={busy ? undefined : onClose} />
      <div className={cn('relative w-full rounded-2xl border border-zinc-800 bg-zinc-900 shadow-2xl shadow-black/60', width)}>
        <div className="flex items-start justify-between gap-4 px-6 pt-5">
          <h2 className="text-base font-semibold text-zinc-100">{title}</h2>
          <button onClick={onClose} disabled={busy} aria-label="Close"
                  className="-mr-2 -mt-1 rounded-md p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="px-6 py-4">{children}</div>
        {footer && <div className="flex items-center justify-end gap-2 border-t border-zinc-800/80 px-6 py-3.5">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

export function Button({ variant = 'ghost', className, children, ...props }) {
  const styles = {
    ghost: 'text-zinc-300 hover:bg-zinc-800',
    solid: 'bg-accent text-accent-foreground hover:bg-accent-hi font-semibold',
    danger: 'bg-red-600 text-white hover:bg-red-500 font-semibold',
    outline: 'border border-zinc-700 text-zinc-200 hover:bg-zinc-800',
  }[variant];
  return (
    <button {...props}
            className={cn('inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm transition disabled:cursor-not-allowed disabled:opacity-50', styles, className)}>
      {children}
    </button>
  );
}

// ------------------------------------------------------------------ name prompt
export function NameDialog({ open, title, label, initial = '', confirmLabel = 'Save', selectStem = false, onSubmit, onClose }) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    setValue(initial); setError(''); setBusy(false);
    const t = setTimeout(() => {
      const el = ref.current;
      if (!el) return;
      el.focus();
      const dot = initial.lastIndexOf('.');
      el.setSelectionRange(0, selectStem && dot > 0 ? dot : initial.length);
    }, 30);
    return () => clearTimeout(t);
  }, [open, initial, selectStem]);

  const submit = async (e) => {
    e?.preventDefault();
    const v = value.trim();
    if (!v) { setError('Give it a name.'); return; }
    if (/[<>:"/\\|?*]/.test(v)) { setError('Names cannot contain  < > : " / \\ | ? *'); return; }
    setBusy(true);
    try { await onSubmit(v); } catch (err) { setError(err.message); setBusy(false); }
  };

  return (
    <Modal open={open} onClose={onClose} title={title} busy={busy}
           footer={<>
             <Button onClick={onClose} disabled={busy}>Cancel</Button>
             <Button variant="solid" onClick={submit} disabled={busy}>{busy && <Loader2 className="h-4 w-4 animate-spin" />}{confirmLabel}</Button>
           </>}>
      <form onSubmit={submit}>
        {label && <label className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-zinc-500">{label}</label>}
        <input ref={ref} value={value} onChange={(e) => { setValue(e.target.value); setError(''); }}
               spellCheck={false} autoComplete="off"
               className={cn('w-full rounded-lg border bg-zinc-950 px-3 py-2.5 text-sm text-zinc-100 outline-none transition focus:ring-2',
                 error ? 'border-red-500/60 focus:ring-red-500/30' : 'border-zinc-700 focus:border-accent focus:ring-accent/30')} />
        {error && <p className="mt-2 flex items-start gap-1.5 text-xs text-red-400"><AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />{error}</p>}
      </form>
    </Modal>
  );
}

// ------------------------------------------------------------------ confirm
export function ConfirmDialog({ open, title, message, confirmLabel = 'Confirm', danger = false, onConfirm, onClose }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { if (open) { setBusy(false); setError(''); } }, [open]);
  const go = async () => {
    setBusy(true);
    try { await onConfirm(); } catch (e) { setError(e.message); setBusy(false); }
  };
  return (
    <Modal open={open} onClose={onClose} title={title} busy={busy}
           footer={<>
             <Button onClick={onClose} disabled={busy}>Cancel</Button>
             <Button variant={danger ? 'danger' : 'solid'} onClick={go} disabled={busy}>
               {busy && <Loader2 className="h-4 w-4 animate-spin" />}{confirmLabel}
             </Button>
           </>}>
      <div className="text-sm leading-relaxed text-zinc-400">{message}</div>
      {error && <p className="mt-3 text-xs text-red-400">{error}</p>}
    </Modal>
  );
}

// ------------------------------------------------------------------ folder picker
function PickNode({ node, depth, selected, onSelect, disabled, expanded, toggle, childrenOf }) {
  const isOpen = expanded.has(node.path);
  const kids = childrenOf[node.path];
  const off = disabled(node.path);
  return (
    <div>
      <div className={cn('group flex items-center gap-1 rounded-lg pr-2 text-sm', selected === node.path ? 'bg-accent/15 text-accent-hi' : 'text-zinc-300 hover:bg-zinc-800/70',
                         off && 'opacity-40')}
           style={{ paddingLeft: 6 + depth * 16 }}>
        <button type="button" aria-label={isOpen ? 'Collapse' : 'Expand'} onClick={() => toggle(node)}
                className={cn('grid h-6 w-6 place-items-center rounded text-zinc-500 hover:text-zinc-200', !node.has_children && 'invisible')}>
          {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </button>
        <button type="button" disabled={off} onClick={() => onSelect(node.path)} onDoubleClick={() => toggle(node)}
                className="flex min-w-0 flex-1 items-center gap-2 py-1.5 text-left">
          <Folder className="h-4 w-4 shrink-0 fill-amber-300/25 text-amber-300" strokeWidth={1.75} />
          <span className="truncate">{node.name}</span>
        </button>
      </div>
      {isOpen && (kids === undefined
        ? <div className="py-1 pl-12 text-xs text-zinc-600">Loading...</div>
        : kids.map((k) => (
          <PickNode key={k.path} node={k} depth={depth + 1} selected={selected} onSelect={onSelect}
                    disabled={disabled} expanded={expanded} toggle={toggle} childrenOf={childrenOf} />
        )))}
    </div>
  );
}

export function FolderPicker({ open, title, confirmLabel = 'Move here', excluded = [], startAt = '', onPick, onClose }) {
  const [selected, setSelected] = useState('');
  const [expanded, setExpanded] = useState(new Set());
  const [childrenOf, setChildrenOf] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = async (p) => {
    try {
      const r = await filesApi.tree(p);
      setChildrenOf((c) => ({ ...c, [p]: r.folders }));
    } catch (e) { setError(e.message); }
  };

  useEffect(() => {
    if (!open) return;
    setSelected(startAt); setBusy(false); setError(''); setChildrenOf({});
    // open the trail down to where you are now, so "move here" starts near you
    const trail = new Set(['']);
    let acc = '';
    startAt.split('/').filter(Boolean).forEach((seg) => { trail.add(acc); acc = joinPath(acc, seg); });
    trail.add(acc);
    setExpanded(trail);
    [...trail].forEach(load);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const toggle = (node) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(node.path)) next.delete(node.path);
      else { next.add(node.path); if (childrenOf[node.path] === undefined) load(node.path); }
      return next;
    });
  };

  const disabled = (p) => excluded.some((x) => p === x || p.startsWith(`${x}/`));
  const root = { name: 'All files', path: '', has_children: true };

  const confirm = async () => {
    setBusy(true);
    try { await onPick(selected); } catch (e) { setError(e.message); setBusy(false); }
  };

  return (
    <Modal open={open} onClose={onClose} title={title} busy={busy} width="max-w-lg"
           footer={<>
             <Button onClick={onClose} disabled={busy}>Cancel</Button>
             <Button variant="solid" onClick={confirm} disabled={busy || disabled(selected)}>
               {busy && <Loader2 className="h-4 w-4 animate-spin" />}{confirmLabel}
             </Button>
           </>}>
      <div className="max-h-[50vh] overflow-y-auto rounded-xl border border-zinc-800 bg-zinc-950/60 p-2">
        <div className={cn('flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm', selected === '' ? 'bg-accent/15 text-accent-hi' : 'text-zinc-300 hover:bg-zinc-800/70')}>
          <button type="button" onClick={() => setSelected('')} className="flex flex-1 items-center gap-2 text-left">
            <HardDrive className="h-4 w-4 text-zinc-400" /> All files
          </button>
        </div>
        {(childrenOf[''] || []).map((n) => (
          <PickNode key={n.path} node={n} depth={1} selected={selected} onSelect={setSelected} disabled={disabled}
                    expanded={expanded} toggle={toggle} childrenOf={childrenOf} />
        ))}
        {childrenOf[''] && childrenOf[''].length === 0 && <p className="px-3 py-3 text-xs text-zinc-600">There are no folders yet - it will go in the top level.</p>}
      </div>
      <p className="mt-3 text-xs text-zinc-500">
        Destination: <span className="text-zinc-300">{selected ? selected.split('/').join(' / ') : 'All files'}</span>
      </p>
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
    </Modal>
  );
}

// ------------------------------------------------------------------ toasts
export function useToasts() {
  const [toasts, setToasts] = useState([]);
  const idRef = useRef(0);
  const dismiss = (id) => setToasts((t) => t.filter((x) => x.id !== id));
  const push = (text, opts = {}) => {
    const id = ++idRef.current;
    setToasts((t) => [...t.slice(-3), { id, text, tone: opts.tone || 'info', action: opts.action }]);
    setTimeout(() => dismiss(id), opts.action ? 9000 : opts.tone === 'error' ? 7000 : 3500);
    return id;
  };
  return { toasts, push, dismiss };
}

export function Toasts({ toasts, dismiss }) {
  if (!toasts.length) return null;
  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 bottom-5 z-[90] flex flex-col items-center gap-2 px-4" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id}
             className={cn('pointer-events-auto flex max-w-lg items-center gap-3 rounded-xl border px-4 py-2.5 text-sm shadow-xl shadow-black/50 animate-shade-in',
               t.tone === 'error' ? 'border-red-500/40 bg-red-950/90 text-red-100' : 'border-zinc-700 bg-zinc-900/95 text-zinc-100')}>
          {t.tone === 'success' && <Check className="h-4 w-4 shrink-0 text-emerald-400" />}
          {t.tone === 'error' && <AlertTriangle className="h-4 w-4 shrink-0 text-red-400" />}
          <span className="min-w-0 break-words">{t.text}</span>
          {t.action && (
            <button onClick={() => { t.action.onClick(); dismiss(t.id); }}
                    className="shrink-0 rounded-md px-2 py-0.5 text-sm font-semibold text-accent-hi hover:bg-white/5">
              {t.action.label}
            </button>
          )}
          <button onClick={() => dismiss(t.id)} aria-label="Dismiss" className="shrink-0 text-zinc-500 hover:text-zinc-200"><X className="h-3.5 w-3.5" /></button>
        </div>
      ))}
    </div>,
    document.body,
  );
}

// ------------------------------------------------------------------ small dropdown
export function Dropdown({ trigger, items, align = 'left', className }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const close = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', esc);
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc); };
  }, [open]);
  const shown = useMemo(() => items.filter(Boolean), [items]);
  return (
    <div className={cn('relative', className)} ref={ref}>
      <div onClick={() => setOpen((o) => !o)}>{typeof trigger === 'function' ? trigger(open) : trigger}</div>
      {open && (
        <div className={cn('absolute z-40 mt-1.5 min-w-52 rounded-xl border border-zinc-700/60 bg-zinc-900 p-1.5 shadow-2xl shadow-black/60', align === 'right' ? 'right-0' : 'left-0')}>
          {shown.map((it, i) => it.type === 'divider'
            ? <div key={i} className="my-1 border-t border-zinc-800" />
            : (
              <button key={i} disabled={it.disabled} onClick={() => { setOpen(false); it.onClick?.(); }}
                      className={cn('flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition disabled:opacity-40',
                        it.active ? 'bg-zinc-800 text-zinc-50' : 'text-zinc-300 hover:bg-zinc-800/80',
                        it.danger && 'text-red-400 hover:bg-red-500/10')}>
                {it.icon && <it.icon className="h-4 w-4 shrink-0 text-zinc-500" />}
                <span className="flex-1">{it.label}</span>
                {it.hint && <span className="text-xs text-zinc-600">{it.hint}</span>}
                {it.active && <Check className="h-4 w-4 text-accent" />}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}

export { baseName };
