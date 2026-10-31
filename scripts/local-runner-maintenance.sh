#!/usr/bin/env bash
# Only for the dedicated DjedAI-CI Docker daemon: jobs never share this host.
set -euo pipefail

remove_test_containers() {
    local label=$1 output
    output=$(timeout 15 docker ps -aq --filter "label=$label")
    if [[ -n $output ]]; then
        local -a ids
        mapfile -t ids <<< "$output"
        timeout 30 docker rm --force --volumes "${ids[@]}"
    fi
}

cleanup() {
    # SIGKILL and interrupted job reporting can bypass Python's finally blocks.
    remove_test_containers org.djedai.postgres-acceptance
    remove_test_containers org.djedai.clickhouse-acceptance
    remove_test_containers 'org.djedai.compose-acceptance=true'
    local output
    output=$(timeout 15 docker volume ls -q \
        --filter label=org.djedai.compose-acceptance=true \
        --filter label=com.docker.compose.volume=app-data)
    if [[ -n $output ]]; then
        local -a volumes
        mapfile -t volumes <<< "$output"
        timeout 20 docker volume rm "${volumes[@]}"
    fi
    output=$(timeout 15 docker network ls -q --filter label=org.djedai.compose-acceptance=true)
    if [[ -n $output ]]; then
        local -a networks
        mapfile -t networks <<< "$output"
        timeout 20 docker network rm "${networks[@]}"
    fi
}

watchdog() {
    systemctl is-active --quiet djedai-ci || return 0
    local ages oldest
    ages=$(ps -C Runner.Worker -o etimes= || true)
    oldest=$(awk '$1 > max { max = $1 } END { print max + 0 }' <<< "$ages")
    # Longest jobs allow 25 minutes plus cancellation/reporting grace.
    # Do not restart a legitimate job just because the VPN is reconnecting.
    if (( oldest > 2100 )); then
        logger -t djedai-ci-watchdog "Restarting runner: worker exceeded 35 minutes ($oldest seconds)"
        systemctl restart djedai-ci
    fi
}

install_maintenance() {
    [[ $EUID == 0 ]] || { echo 'Installation requires root.' >&2; exit 1; }
    install -d -m 0755 /opt/djedai/hooks /etc/systemd/system/djedai-ci.service.d
    cat > /opt/djedai/hooks/job-cleanup.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec timeout 90 /usr/local/sbin/djedai-ci-maintenance cleanup
EOF
    chmod 0755 /opt/djedai/hooks/job-cleanup.sh
    cat > /etc/systemd/system/djedai-ci.service.d/maintenance.conf <<'EOF'
[Service]
Environment=ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/djedai/hooks/job-cleanup.sh
Environment=ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/djedai/hooks/job-cleanup.sh
EOF
    cat > /etc/systemd/system/djedai-ci-watchdog.service <<'EOF'
[Unit]
Description=Recover DjedAI runner workers stuck beyond job deadlines

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/djedai-ci-maintenance watchdog
TimeoutStartSec=180
EOF
    cat > /etc/systemd/system/djedai-ci-watchdog.timer <<'EOF'
[Unit]
Description=Check DjedAI runner worker deadlines every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
EOF
    if [[ $(ps -p 1 -o comm=) == systemd ]]; then
        systemctl daemon-reload
        systemctl enable --now djedai-ci-watchdog.timer
    else
        systemctl enable djedai-ci-watchdog.timer
    fi
}

case ${1:-} in
    cleanup) cleanup ;;
    watchdog) watchdog ;;
    install) install_maintenance ;;
    *) echo 'Usage: local-runner-maintenance.sh cleanup|watchdog|install' >&2; exit 2 ;;
esac
