#!/usr/bin/env bash
# Pushes this folder to GitHub.
#   bash push-to-github.sh
#
# There is ONE prompt, and it asks for a PASSWORD.
# Paste your fine-grained Personal Access Token there. Nothing appears
# on screen while you paste - that is normal. Press Enter.
set -e

OWNER="${1:-z3r0fx}"
REPO="${2:-zerko-file-manager}"

cd "$(dirname "$0")"

if [ ! -d .git ]; then
    git init -b main
    git add -A
    git commit -m "Zerko File Manager v1.0.0"
fi

# Username lives in the URL so git never asks for it.
git remote remove origin 2>/dev/null || true
git remote add origin "https://$OWNER@github.com/$OWNER/$REPO.git"

# Remember it, so this is the only time you paste the token.
git config credential.helper store

echo
echo "Repo:   https://github.com/$OWNER/$REPO"
echo
echo "You will see ONE prompt:  Password for 'https://$OWNER@github.com':"
echo "Paste the TOKEN there (starts with github_pat_). It stays invisible."
echo

git push -u origin main

echo
echo "Pushed. Tagging v1.0.0..."
git tag -f v1.0.0
git push -f origin v1.0.0
echo
echo "Done. Next: make the Release on GitHub and attach the zip."
