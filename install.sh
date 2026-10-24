#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "boss requires Python 3.12+")'
for target in "$HOME/.local/bin/boss" "$HOME/.agents/skills/boss" "$HOME/.hermes/plugins/boss"; do
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        echo "error: $target exists and is not a symlink" >&2
        exit 2
    fi
done
mkdir -p "$HOME/.local/bin" "$HOME/.agents/skills" "$HOME/.hermes/plugins"
ln -sfn "$root/bin/boss" "$HOME/.local/bin/boss"
ln -sfn "$root/skill" "$HOME/.agents/skills/boss"
ln -sfn "$root/plugin" "$HOME/.hermes/plugins/boss"
echo 'installed: ~/.local/bin/boss'
echo 'installed: ~/.agents/skills/boss'
echo 'installed: ~/.hermes/plugins/boss'
echo 'enable plugin (opt-in): hermes plugins enable boss'
