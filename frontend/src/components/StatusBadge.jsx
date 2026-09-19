import React from 'react';

const statusStyles = {
  raw: 'bg-gray-500 text-white',
  edited: 'bg-blue-500 text-white',
  graded: 'bg-purple-500 text-white',
  delivered: 'bg-green-500 text-white',
  archived: 'bg-yellow-600 text-white'
};

export default function StatusBadge({ status, className = '' }) {
  const style = statusStyles[status] || statusStyles.raw;
  
  return (
    <span className={`px-2 py-1 rounded text-xs font-semibold uppercase ${style} ${className}`}>
      {status}
    </span>
  );
}
