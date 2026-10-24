#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$root"
exec "${BOSS_PYTHON:-python3}" -m boss.setup "$@"
