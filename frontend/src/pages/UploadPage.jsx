import { apiCall } from '../lib/api';
import { useState, useContext } from 'react';
import { useNavigate } from 'react-router-dom';
import { Cloud, Upload, FolderUp } from 'lucide-react';
import { DataContext } from '../context/DataContext';

export default function UploadPage() {
  const navigate = useNavigate();
  const { addToQueue } = useContext(DataContext);

  const handleDrop = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      await processFiles(e.dataTransfer.files);
    }
  };

  const processFiles = async (files, { isFolder = false } = {}) => {
      const fileList = Array.from(files).filter((f) => f.size > 0);
      let filesToUpload = fileList;

      // A directory upload can be hundreds of files - checking each one for
      // duplicates would be hundreds of round trips before anything starts.
      if (!isFolder) {
        filesToUpload = [];
        for (const file of fileList) {
          try {
            const check = await apiCall('/api/check-duplicate', {
              method: 'POST',
              body: JSON.stringify({ filename: file.name, file_size: file.size })
            });
            if (check.is_duplicate) {
              if (confirm(`A file named "${file.name}" already exists. Upload anyway?`)) {
                filesToUpload.push(file);
              }
            } else {
              filesToUpload.push(file);
            }
          } catch {
            filesToUpload.push(file);
          }
        }
      }

      if (filesToUpload.length > 0) {
        if (isFolder) {
          const top = filesToUpload[0].webkitRelativePath?.split('/')[0] || 'folder';
          const folders = new Set(
            filesToUpload.map((f) => (f.webkitRelativePath || '').split('/').slice(0, -1).join('/'))
          );
          if (!confirm(
            `Upload "${top}" — ${filesToUpload.length} files across ${folders.size} folder(s)?\n\n` +
            `The folder structure will be recreated inside the project you're in.`
          )) return;
        }
        addToQueue(filesToUpload);
        navigate('/browse');
      }
  };

  const handleFileSelect = async (e) => {
    if (e.target.files && e.target.files.length > 0) {
      await processFiles(e.target.files);
    }
    e.target.value = '';
  };

  const handleFolderSelect = async (e) => {
    if (e.target.files && e.target.files.length > 0) {
      await processFiles(e.target.files, { isFolder: true });
    }
    e.target.value = '';
  };

  return (
    <div className="flex h-full">
<div className="relative flex flex-col items-center justify-center flex-1 p-6">
        <div className="w-full max-w-2xl text-center">
          <h1 className="text-3xl font-bold text-zinc-100 mb-8">Upload Media</h1>
          
          <label 
            className="flex flex-col items-center justify-center w-full h-64 border-2 border-zinc-700 border-dashed rounded-lg cursor-pointer bg-zinc-900 hover:bg-zinc-800 transition"
            onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); }}
            onDrop={handleDrop}
          >
            <div className="flex flex-col items-center justify-center pt-5 pb-6">
              <Upload className="w-12 h-12 text-zinc-500 mb-4" />
              <p className="mb-2 text-sm text-zinc-400 font-semibold">Click to upload or drag and drop</p>
              <p className="text-xs text-zinc-500">Multiple files supported</p>
            </div>
            <input type="file" className="hidden" multiple onChange={handleFileSelect} />
          </label>

          <div className="mt-6 flex flex-col items-center gap-3">
            <div className="flex items-center gap-3 w-full">
              <div className="h-px bg-zinc-800 flex-1" />
              <span className="text-xs text-zinc-600">or</span>
              <div className="h-px bg-zinc-800 flex-1" />
            </div>

            <label className="inline-flex items-center gap-2.5 px-5 py-3 rounded-lg border border-zinc-700 bg-zinc-900 hover:bg-zinc-800 hover:border-[#ff5c1f]/60 cursor-pointer transition">
              <FolderUp className="w-5 h-5 text-[#ff5c1f]" />
              <span className="text-sm font-medium text-zinc-200">Upload a whole folder</span>
              <input
                type="file"
                className="hidden"
                multiple
                webkitdirectory=""
                directory=""
                onChange={handleFolderSelect}
              />
            </label>

            <p className="text-xs text-zinc-500 max-w-md leading-relaxed">
              Keeps the structure. A folder holding <span className="text-zinc-400">Drone</span>,{' '}
              <span className="text-zinc-400">Sony</span> and <span className="text-zinc-400">Meta</span>
              {' '}subfolders arrives with those subfolders intact, inside whichever project you&rsquo;re
              currently viewing.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}