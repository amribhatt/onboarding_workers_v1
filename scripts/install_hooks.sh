#!/bin/sh
# One-time, per clone: make `git merge` / `git pull` on main refresh the README (scripts/update_readme.py).
# Hooks are not versioned, so this adds one line to .git/hooks/post-merge and keeps any hook already there.
set -e
root=$(git rev-parse --show-toplevel)
hook="$root/.git/hooks/post-merge"
line='sh "$(git rev-parse --show-toplevel)/scripts/hooks/post-merge-readme"'
[ -f "$hook" ] || printf '#!/bin/sh
' > "$hook"
# our line goes before any `exec` the hook already has, or it would never run
if ! grep -qF 'post-merge-readme' "$hook"; then
  { head -n 1 "$hook"; printf '%s
' "$line"; tail -n +2 "$hook"; } > "$hook.new" && mv "$hook.new" "$hook"
fi
chmod +x "$hook" "$root/scripts/hooks/post-merge-readme"
echo "post-merge hook installed: $hook"
