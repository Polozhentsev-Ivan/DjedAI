#!/usr/bin/env bash
# Bootstrap a dedicated, disposable Ubuntu 24.04 WSL distribution as root.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
runner_url=${1:?runner download URL required}
runner_sha256=${2:?runner SHA256 required}
[[ $runner_url == https://github.com/actions/runner/releases/download/* ]]
[[ $runner_sha256 =~ ^[a-f0-9]{64}$ ]]
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]]

apt-get update
apt-get install -y ca-certificates curl git gnupg jq sudo unzip zip xz-utils systemd-sysv dbus locales
locale-gen en_US.UTF-8
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
id djedai >/dev/null 2>&1 || useradd --create-home --shell /bin/bash djedai
usermod -aG docker djedai
echo 'djedai ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/djedai-ci
chmod 0440 /etc/sudoers.d/djedai-ci
visudo -cf /etc/sudoers.d/djedai-ci
install -d -o djedai -g djedai /opt/djedai/actions-runner /opt/djedai/hooks
if [[ ! -x /opt/djedai/actions-runner/run.sh ]]; then
    archive=$(mktemp)
    trap 'rm -f "$archive"' EXIT
    curl --fail --location --retry 3 "$runner_url" -o "$archive"
    printf '%s  %s\n' "$runner_sha256" "$archive" | sha256sum --check
    tar -xzf "$archive" -C /opt/djedai/actions-runner
    chown -R djedai:djedai /opt/djedai/actions-runner
fi
/opt/djedai/actions-runner/bin/installdependencies.sh

# No Windows drive mounts, Windows executables or Docker Desktop socket in CI.
cat > /etc/wsl.conf <<'EOF'
[boot]
systemd=true
[automount]
enabled=false
mountFsTab=false
[interop]
enabled=false
appendWindowsPath=false
[user]
default=djedai
EOF

cat > /etc/systemd/system/djedai-ci.service <<'EOF'
[Unit]
Description=DjedAI GitHub Actions runner
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service
ConditionPathExists=/opt/djedai/actions-runner/.runner

[Service]
User=djedai
Group=djedai
SupplementaryGroups=docker
WorkingDirectory=/opt/djedai/actions-runner
Environment=LANG=en_US.UTF-8
Environment=DOTNET_CLI_TELEMETRY_OPTOUT=1
ExecStart=/opt/djedai/actions-runner/run.sh
Restart=always
RestartSec=10
KillMode=control-group
TimeoutStopSec=120
CPUAffinity=0-7

[Install]
WantedBy=multi-user.target
EOF
cat > /usr/local/sbin/djedai-ci-register <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
read -r token
token=${token//$'\r'/}
cd /opt/djedai/actions-runner
if [[ ! -f .runner ]]; then
    runuser -u djedai -- ./config.sh --unattended --url "$1" --token "$token" --name "$2" --labels djedai-ci --work _work
fi
unset token
systemctl daemon-reload
systemctl enable --now docker djedai-ci
runuser -u djedai -- docker info --format '{{.OSType}} {{.ServerVersion}}'
runuser -u djedai -- docker compose version
EOF
chmod 0755 /usr/local/sbin/djedai-ci-register
cat > /usr/local/sbin/djedai-ci-keepalive <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
systemctl start docker djedai-ci
# systemd services alone do not keep a WSL distribution alive.
exec /usr/bin/sleep infinity
EOF
chmod 0755 /usr/local/sbin/djedai-ci-keepalive
systemctl enable docker djedai-ci
echo 'Linux installation complete. Restart this distribution before registration.'
