# Before you run `npm run build`

`dist/` is the authoritative frontend in this release. The photo
editor was authored directly in the compiled bundle and `src/` has
not caught up with it yet, so rebuilding from `src/` will produce an
app with the editor missing.

Rebuild only once `src/` contains the editor, and check the built
bundle still contains `Save this look as` before shipping it.
