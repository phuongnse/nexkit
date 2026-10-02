#!/bin/bash
# Only for an empty Ubuntu 24.04 distro in the Windows CI acceptance job.
set -euo pipefail
test "$(cat /proc/1/comm)" = systemd
apt-get update
apt-get install -y ca-certificates curl git python3 python3-venv sudo iptables
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<'REPOSITORY'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
REPOSITORY
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io
systemctl enable --now docker
docker info --format '{{json .SecurityOptions}} {{.OSType}} {{.Architecture}} {{.KernelVersion}}'
