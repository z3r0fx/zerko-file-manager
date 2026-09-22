## v1.2.0 — 2026-09-22

### Accounts and permissions
- Three roles. **Editors** can sort, move, rename, upload, download, run
  transcriptions and manage portals, and can create client accounts — but
  cannot start or stop proxy jobs, cannot hard-delete, and cannot create
  other editors. **Clients/agents** can upload, download, create, rename and
  sort, with no hard delete and no admin dashboard.
- Deleting now means the Trash. Nothing is dropped from the catalogue
  outright except by an admin.
- Every route is checked against one capability table, at one place in the
  request path.

### Portals
- Send portals and receive portals are now separate things, created from
  "Create portal".
- A confirm step, so you can see what you typed and ticked was actually sent.
- Optional zip-on-download, and an intake panel for whatever arrives.

### Files
- Documents and other non-media files can be uploaded and browsed alongside
  footage, laid out like a file manager.

### Photo editor
- Auto grading is anchored to the histogram: nothing clipped at either end,
  roughly 1–3% off the extremes.
- Eight rebuilt starter looks, balanced the same way.
- Colour mixer with per-hue saturation, defringe, and PNG watermarks you can
  apply across a whole folder.
- Auto straighten and keystone from detected lines.
- Tap a slider's number to type an exact value.

### Phone
- The whole app reflows for a phone. Pinch to zoom in the editor, a
  scrollable tool bar, and a QR panel for uploading from your phone.

### Setup
- A fresh install now opens a setup wizard: create the first account, pick
  where your media lives, done. No terminal, no config file.

### Updating

If you already have Zerko, open **Dashboard → Updates** and press
Download, then Install and restart. Your library, settings and login
are not touched.

First time? Download the zip below, unpack it, and run `Start Zerko.bat`.
