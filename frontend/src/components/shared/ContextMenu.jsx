import { useEffect, useRef, useState } from 'react';
import { cn } from '../../lib/utils';

export default function ContextMenu({ x, y, items, onClose }) {
  const menuRef = useRef(null);
  const [activeSubmenu, setActiveSubmenu] = useState(null);
  
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        onClose();
      }
    };
    const handleEscape = (e) => e.key === 'Escape' && onClose();

    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleEscape);

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [onClose]);

  const adjustedX = Math.min(x, window.innerWidth - 200);
  const adjustedY = Math.min(y, window.innerHeight - 300);

  return (
    <div
      ref={menuRef}
      className="fixed z-50 bg-zinc-900 border border-zinc-700/50 rounded-xl shadow-2xl shadow-black/60 py-1.5 min-w-48"
      style={{ left: adjustedX, top: adjustedY }}
    >
      {items.map((item, index) => {
        if (item.type === 'divider') {
          return <div key={index} className="border-t border-zinc-700/40 my-1 mx-3" />;
        }

        const Icon = item.icon;
        const hasSubmenu = item.submenu && item.submenu.length > 0;

        return (
          <div key={index} className="relative group">
            <button
              onClick={() => {
                if (hasSubmenu) {
                  setActiveSubmenu(activeSubmenu === index ? null : index);
                } else {
                  item.onClick();
                  onClose();
                }
              }}
              className={cn(
                "w-full flex items-center justify-start gap-3 px-4 py-2.5 text-sm rounded-lg mx-1 transition-all duration-150 text-left",
                item.danger 
                  ? 'text-red-400 hover:bg-red-500/10 hover:text-red-300' 
                  : 'text-zinc-200 hover:bg-zinc-700/60'
              )}
            >
              <div className="flex items-center gap-3">
                {Icon && <Icon className={cn("w-4 h-4", item.danger ? 'text-red-400' : 'text-zinc-400')} />}
                <span>{item.label}</span>
              </div>
            </button>

            {hasSubmenu && activeSubmenu === index && (
              <div className="absolute left-full top-0 ml-1 bg-zinc-900 border border-zinc-700/50 rounded-xl shadow-2xl shadow-black/60 py-1.5 min-w-48">
                {item.submenu.map((sub, si) => (
                  <button
                    key={si}
                    onClick={(e) => {
                      e.stopPropagation();
                      sub.onClick();
                      onClose();
                    }}
                    className="w-full flex items-center gap-3 px-4 py-2.5 text-sm text-zinc-200 hover:bg-zinc-700/60 rounded-lg mx-1 transition-all duration-150 text-left"
                  >
                    {sub.icon && <sub.icon className="w-4 h-4 text-zinc-400" />}
                    <span>{sub.label}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
