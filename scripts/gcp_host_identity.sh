#!/usr/bin/env bash
# Emit ONLY public host identity into authenticated GCE serial output.
# No provider credentials, private keys or application configuration belong here.
set -euo pipefail
printf 'GCA_HOST_IDENTITY_BEGIN\n'
sed -n '1p' /etc/ssh/ssh_host_ed25519_key.pub
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
printf 'GCA_HOST_IDENTITY_END\n'
