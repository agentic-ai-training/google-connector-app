#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cargo install --locked --path "$repository_root/coding_runtime" --bin gca-local
gca-local doctor --workspace "$repository_root"
