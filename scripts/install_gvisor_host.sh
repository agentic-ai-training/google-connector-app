#!/usr/bin/env bash
# Trusted operator provisioning only; never expose this script as a model tool.
# Adds runsc, preserving the default Docker runtime and existing containers.
set -euo pipefail
umask 077
test "$(id -u)" -eq 0
test "$(dpkg --print-architecture)" = amd64
command -v docker >/dev/null
command -v curl >/dev/null
command -v gpg >/dev/null
test "$(systemctl show docker --property=CanReload --value)" = yes

state=/var/lib/gca-runtime-install
install -d -m 0700 "$state"
exec 9>"$state/lock"
flock -n 9
if test -f "$state/complete"; then
    printf 'Already provisioned; verify evidence instead of reinstalling.\n'
    exit 0
fi
if command -v runsc >/dev/null; then
    printf 'Existing runsc detected; explicit reconciliation is required.\n' >&2
    exit 1
fi
docker info --format '{{.DefaultRuntime}}' >"$state/default-runtime.before"
docker ps --format '{{.ID}}' | sort >"$state/containers.before"
if test -f /etc/docker/daemon.json; then
    cp -p /etc/docker/daemon.json "$state/daemon.before.json"
fi
curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
    --max-time 60 https://gvisor.dev/archive.key -o "$state/archive.key"
gpg --batch --yes --dearmor --output "$state/archive-keyring.gpg" "$state/archive.key"
install -m 0644 "$state/archive-keyring.gpg" /usr/share/keyrings/gvisor-archive-keyring.gpg
printf '%s\n' 'deb [arch=amd64 signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main' \
    > /etc/apt/sources.list.d/gvisor.list
apt-get update -qq
version=$(apt-cache policy runsc | awk '/Candidate:/ {print $2}')
test -n "$version"
test "$version" != '(none)'
printf '%s\n' "$version" >"$state/package-version"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "runsc=$version"
# Runtime upgrades are reviewed, not silently inherited from apt upgrade.
apt-mark hold runsc
runsc --version >"$state/runsc-version"
sha256sum "$(command -v runsc)" >"$state/runsc-sha256"
if test -f /etc/docker/daemon.json; then
    dockerd --validate --config-file=/etc/docker/daemon.json
fi
systemctl daemon-reload
systemctl reload docker
docker info --format '{{.DefaultRuntime}}' >"$state/default-runtime.after"
cmp "$state/default-runtime.before" "$state/default-runtime.after"
docker info --format '{{json .Runtimes}}' | python3 -c \
    'import json,sys; assert "runsc" in json.load(sys.stdin), "runsc not registered"'
while IFS= read -r container; do
    test "$(docker inspect --format '{{.State.Running}}' "$container")" = true
done <"$state/containers.before"
date -u +%FT%TZ >"$state/complete"
printf 'runsc registered; default runtime preserved; prior containers remain running.\n'
sed -n '1,2p' "$state/runsc-version"
sed -n '1p' "$state/runsc-sha256"
