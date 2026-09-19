#!/usr/bin/env bash
# Pushes this folder to GitHub. Run it from WSL:
#   cd /mnt/c/Users/$USER_WINDOWS_NAME/odysseus/data/zerko-installer/repo
#   bash push-to-github.sh
#
# You will be asked for your GitHub username and a Personal Access Token
# (the token is the password - your normal GitHub password will NOT work).
# Make a token at: https://github.com/settings/tokens?type=beta
#   - Repository access: Only select repositories -> zerko-file-manager
#   - Permissions: Contents = Read and write
set -e

OWNER="${1:-z3r0fx}"
REPO="${2:-zerko-file-manager}"

cd "$(dirname "$0")"

if [ ! -d .git ]; then
    git init -b main
    git add -A
    git commit -m "Zerko File Manager v1.0.0"
fi

git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/$OWNER/$REPO.git"

echo
echo "Pushing to https://github.com/$OWNER/$REPO"
echo "Username: $OWNER"
echo "Password: paste your Personal Access Token (it stays hidden as you paste)"
echo
git push -u origin main

echo
echo "Done. Now tag the release:"
echo "  git tag v1.0.0 && git push origin v1.0.0"
