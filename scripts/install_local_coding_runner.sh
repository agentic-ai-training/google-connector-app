#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cargo install --locked --path "$repository_root/coding_runtime" --bin gca-local
installed_binary="${CARGO_HOME:-$HOME/.cargo}/bin/gca-local"
"$installed_binary" doctor --workspace "$repository_root"

case ":${PATH:-}:" in
    *:"$(dirname -- "$installed_binary")":*) ;;
    *) printf '%s\n' "Installed successfully. Add $(dirname -- "$installed_binary") to PATH to invoke gca-local by name." ;;
esac
