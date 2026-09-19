import { useState, useRef, useEffect, useLayoutEffect } from 'react';
import { createPortal } from 'react-dom';

const STATUS_CONFIG = {
  raw: { label: 'Raw', className: 'bg-amber-500/20 text-amber-400 border-amber-500/30' },
  edited: { label: 'Edited', className: 'bg-blue-500/20 text-blue-400 border-blue-500/30' },
  delivered: { label: 'Delivered', className: 'bg-green-500/20 text-green-400 border-green-500/30' },
  archived: { label: 'Archived', className: 'bg-zinc-500/20 text-zinc-400 border-zinc-500/30' },
};

const STATUS_ORDER = ['raw', 'edited', 'delivered', 'archived'];

export default function StatusBadge({ status, onChange, size = 'sm', onOpen, onClose }) {
  const [open, setOpen] = useState(false);
  const [isUpdating, setIsUpdating] = useState(false);
  const [coords, setCoords] = useState(null);
  const buttonRef = useRef(null);
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.raw;

  const handleChange = async (newStatus) => {
    setIsUpdating(true);
    await onChange(newStatus);
    setTimeout(() => setIsUpdating(false), 500);
  };

  const handleClick = (e) => {
    e.stopPropagation();
    const nextOpen = !open;
    if (nextOpen && buttonRef.current) {
      const rect = buttonRef.current.getBoundingClientRect();
      setCoords({
        top: rect.bottom + window.scrollY + 4,
        left: rect.left + window.scrollX,
      });
    }
    setOpen(nextOpen);
    if (nextOpen) onOpen?.();
    else onClose?.();
  };

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (open && !e.target.closest('.status-dropdown')) {
        setOpen(false);
        onClose?.();
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open, onClose]);

  const sizeClasses = size === 'sm'
    ? 'px-2 py-0.5 text-xs'
    : 'px-3 py-1 text-sm';

  if (!onChange) {
    return (
      <span className={`inline-flex items-center rounded-full border font-medium ${sizeClasses} ${config.className}`}>
        {config.label}
      </span>
    );
  }

  return (
    <>
      <button
        ref={buttonRef}
        onClick={handleClick}
        className={`inline-flex items-center rounded-full border font-medium cursor-pointer transition hover:opacity-90 ${sizeClasses} ${config.className} ${isUpdating ? 'animate-pulse ring-2 ring-white/20' : ''}`}
      >
        {config.label}
        <span className="ml-1 opacity-60">▾</span>
      </button>

      {open && coords && createPortal(
        <div
          className="status-dropdown fixed z-[9999] w-32 bg-zinc-900 border border-zinc-700 rounded-lg shadow-xl overflow-hidden"
          style={{ top: coords.top, left: coords.left }}
        >
          {STATUS_ORDER.map((s) => {
            const cfg = STATUS_CONFIG[s];
            const active = s === status;
            return (
              <button
                key={s}
                onClick={(e) => {
                  e.stopPropagation();
                  if (s !== status) handleChange(s);
                  setOpen(false);
                  onClose?.();
                }}
                className={`w-full text-left px-3 py-2 text-xs font-medium transition ${
                  active
                    ? 'bg-zinc-800 text-zinc-100'
                    : 'text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100'
                }`}
              >
                <span className={`inline-block w-2 h-2 rounded-full mr-2 ${cfg.className.split(' ')[0].replace('/20', '')}`} />
                {cfg.label}
              </button>
            );
          })}
        </div>,
        document.body
      )}
    </>
  );
}
