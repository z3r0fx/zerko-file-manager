import { useState, useEffect, useContext } from 'react';
import { DataContext } from '../../context/DataContext';
import { Trash2 } from 'lucide-react';

export default function NotesPanel({ videoId }) {
  const { addNote, deleteNote } = useContext(DataContext);
  const [notes, setNotes] = useState([]);
  const [newNote, setNewNote] = useState('');
  const [loading, setLoading] = useState(true);

  const fetchNotes = async () => {
    setLoading(true);
    try {
        const response = await fetch(`/api/videos/${videoId}/notes`, {
            headers: { 'Authorization': `Bearer ${localStorage.getItem('token')}` }
        });
        const data = await response.json();
        setNotes(data);
    } catch (err) {
        console.error('Failed to load notes', err);
    } finally {
        setLoading(false);
    }
  };

  useEffect(() => {
    fetchNotes();
  }, [videoId]);

  const handleAddNote = async () => {
    if (!newNote.trim()) return;
    await addNote(videoId, newNote);
    setNewNote('');
    fetchNotes();
  };

  const handleDeleteNote = async (noteId) => {
    await deleteNote(noteId);
    fetchNotes();
  };

  return (
    <div className="bg-zinc-800 rounded-lg p-4">
        <h3 className="text-sm font-semibold text-zinc-300 mb-3">Notes</h3>
        <div className="flex gap-2 mb-4">
            <input 
                type="text" 
                value={newNote} 
                onChange={(e) => setNewNote(e.target.value)} 
                className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-100"
                placeholder="Add a note..."
            />
            <button onClick={handleAddNote} className="px-3 py-1.5 bg-red-600 text-white rounded text-sm hover:bg-red-500">Add</button>
        </div>
        <div className="space-y-3 max-h-40 overflow-y-auto">
            {notes.map(note => (
                <div key={note.id} className="p-2 bg-zinc-900 rounded text-xs text-zinc-300 flex justify-between">
                    <div>
                        <p className="font-semibold text-zinc-400">{note.author} - {new Date(note.created_at).toLocaleString()}</p>
                        <p className="mt-1">{note.content}</p>
                    </div>
                    <button onClick={() => handleDeleteNote(note.id)} className="text-zinc-500 hover:text-red-400"><Trash2 className="w-4 h-4" /></button>
                </div>
            ))}
        </div>
    </div>
  );
}
