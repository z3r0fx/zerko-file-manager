import { X } from 'lucide-react';

export default function ConfirmDialog({ open, title, message, error, onConfirm, onCancel,
                                        extraOption = null }) {
  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onCancel} />
      <div className="relative bg-zinc-900 border border-zinc-700 rounded-xl p-6 max-w-md w-full mx-4 shadow-2xl">
        <button
          onClick={onCancel}
          className="absolute top-4 right-4 text-zinc-400 hover:text-zinc-100 transition"
        >
          <X className="w-5 h-5" />
        </button>

        <h2 className="text-lg font-semibold text-zinc-100 mb-2">{title}</h2>
        <p className="text-zinc-400 mb-4">{message}</p>

        {extraOption && (
          <label className="flex items-start gap-2.5 mb-4 p-3 rounded-lg bg-red-950/30 border border-red-900/50 cursor-pointer">
            <input
              type="checkbox"
              checked={extraOption.checked}
              onChange={(e) => extraOption.onChange(e.target.checked)}
              className="mt-0.5 accent-red-600"
            />
            <span>
              <span className="block text-sm text-red-200">{extraOption.label}</span>
              <span className="block text-xs text-red-400/70 mt-0.5">{extraOption.hint}</span>
            </span>
          </label>
        )}

        {error && (
          <div className="mb-4 text-sm bg-red-900/20 border border-red-700 text-red-300 rounded-lg p-3">
            {error}
          </div>
        )}

        <div className="flex gap-3 justify-end">
          <button
            onClick={onCancel}
            className="px-4 py-2 rounded-lg bg-zinc-800 text-zinc-300 hover:bg-zinc-700 transition font-medium"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-2 rounded-lg bg-red-600 text-white hover:bg-red-500 transition font-medium"
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}