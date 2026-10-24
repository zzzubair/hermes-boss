#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "boss requires Python 3.12+")'
mkdir -p "$HOME/.local/bin"
if [ -e "$HOME/.local/bin/boss" ] && [ ! -L "$HOME/.local/bin/boss" ]; then
    echo 'error: ~/.local/bin/boss exists and is not a symlink' >&2
    exit 2
fi
ln -sfn "$root/bin/boss" "$HOME/.local/bin/boss"
echo 'installed: ~/.local/bin/boss'
