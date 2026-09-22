# Zerko File Manager

A self-hosted catalogue for video work. It reads a folder of footage and makes
it searchable — including by what is **said on camera** — then lets you send
clips to clients with a link.

Your files are never moved, renamed or altered. It only reads them.

Current version: **v1.2.0**

## What it does

- Catalogues a drive of footage: thumbnails, durations, resolution, codec, camera
- Transcribes speech and makes every word searchable, with jump-to-timecode
- Tags clips automatically from folders, camera and what is said
- Finds true duplicates by content, not filename
- Generates small streaming proxies so remote viewing is quick
- Share links for clients: they pick favourites and leave notes, no account needed
- Exports those picks as FCPXML straight into DaVinci Resolve
- Nightly backups of the catalogue
- Updates itself from this repo

## Requirements

- Windows with WSL (Ubuntu). If `wsl -- echo works` fails, run
  `wsl --install -d Ubuntu` from an admin terminal and restart.
- About 1% of your library size in free space, for proxies.
- Transcription is optional and wants an NVIDIA GPU.

## Install

1. Download the latest `.zip` from [Releases](../../releases/latest)
2. Unpack it anywhere, e.g. `C:\Zerko`
3. Run **Start Zerko.bat**
4. Answer three questions in the browser: your account, your footage folder,
   and what should run

The first start installs what it needs inside WSL. That takes a few minutes,
once.

## Updating

Dashboard → Updates → Download → Install and restart.

Updates never touch your catalogue, your settings, your login or your media.
If a new version fails to start, the previous one is restored automatically.

## Licence

Do what you like with it. No warranty — it manages files you care about, so
keep your own backups too.
