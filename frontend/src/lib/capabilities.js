/**
 * The capability names the server uses, so the UI does not spell them as
 * loose strings in twenty files. These must match permissions.py.
 */
export const CAP = {
  READ: 'media.read',
  DOWNLOAD: 'media.download',
  UPLOAD: 'media.upload',
  ORGANISE: 'media.organise',
  ANNOTATE: 'media.annotate',
  TRASH: 'media.trash',
  HARD_DELETE: 'media.harddelete',
  TRANSCRIBE: 'jobs.transcribe',
  PROXY: 'jobs.proxy',
  SHARES: 'shares.manage',
  CREATE_CLIENTS: 'users.create_clients',
  ADMIN: 'admin',
};
