#!/usr/bin/env bash
#
# Configures THIS server (Monitoring VM) with Ansible. Run as root, locally:
#
#   git clone https://github.com/divlv/monitoring-vm /opt/monitoring-vm
#   # put your private config into /etc/monitoring-vm/config.yml (see config.sample.yml)
#   /opt/monitoring-vm/run_ansible.sh            # extra arguments go to ansible-playbook,
#   /opt/monitoring-vm/run_ansible.sh --check --diff   # e.g. a dry run
#
# Every log line (console and /var/log/monitoring-vm/run_<RunId>.log) carries a
# timestamp and the RunId of this run.
#
set -euo pipefail

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ANSIBLE_DIR="$PROJECT_DIR/ansible"
CONFIG_FILE="${MONITORING_VM_CONFIG:-/etc/monitoring-vm/config.yml}"
LOG_DIR="/var/log/monitoring-vm"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run this script as root." >&2
  exit 1
fi

# 7 random characters [a-z0-9]; pure bash, so no pipe can trip "pipefail".
gen_run_id() {
  local chars=abcdefghijklmnopqrstuvwxyz0123456789 id="" i
  for i in 1 2 3 4 5 6 7; do
    id+="${chars:RANDOM%36:1}"
  done
  printf '%s' "$id"
}

RUN_ID="$(gen_run_id)"
mkdir -p "$LOG_DIR"
chmod 0700 "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_${RUN_ID}.log"

log() {
  local level="$1"
  shift
  printf '%(%F %T)T %s RunId: %s %s\n' -1 "$level" "$RUN_ID" "$*" | tee -a "$LOG_FILE"
}

# Re-prints every line of a command's output with timestamp and RunId.
prefix_lines() {
  local line
  while IFS= read -r line; do
    printf '%(%F %T)T INFO RunId: %s | %s\n' -1 "$RUN_ID" "$line"
  done
}

run_logged() {
  "$@" 2>&1 | prefix_lines | tee -a "$LOG_FILE"
}

log INFO "Monitoring VM run started. Project: $PROJECT_DIR, private config: $CONFIG_FILE, log: $LOG_FILE"

if [[ ! -f "$CONFIG_FILE" ]]; then
  log ERROR "Private config $CONFIG_FILE not found."
  log ERROR "Copy config.sample.yml to $CONFIG_FILE (root:root, 0600) and fill in your real values."
  exit 1
fi

if ! command -v ansible-playbook >/dev/null 2>&1; then
  log INFO "ansible-core is not installed - installing it from apt."
  export DEBIAN_FRONTEND=noninteractive
  run_logged apt-get update -qq
  if ! apt-cache policy ansible-core | grep -q 'Candidate: [0-9]'; then
    log INFO "ansible-core has no candidate - enabling the 'universe' component."
    run_logged apt-get install -y -qq software-properties-common
    run_logged add-apt-repository -y universe
    run_logged apt-get update -qq
  fi
  run_logged apt-get install -y -qq ansible-core
fi

# The firewall step refuses to run if the IP of this SSH session is not in "trusted_ips".
SSH_CLIENT_IP="${SSH_CONNECTION:-}"
SSH_CLIENT_IP="${SSH_CLIENT_IP%% *}"
if [[ -z "$SSH_CLIENT_IP" ]]; then
  log WARNING "No SSH session detected (console login?): the firewall lockout check will be skipped."
else
  log INFO "SSH client IP of this session: $SSH_CLIENT_IP"
fi

export ANSIBLE_CONFIG="$ANSIBLE_DIR/ansible.cfg"
export ANSIBLE_NOCOLOR=1
cd "$ANSIBLE_DIR"

rc=0
run_logged ansible-playbook playbooks/main.yml \
  -e "run_id=$RUN_ID" \
  -e "config_file=$CONFIG_FILE" \
  -e "ssh_client_ip=$SSH_CLIENT_IP" \
  -e "log_dir=$LOG_DIR" \
  "$@" || rc=$?

if [[ $rc -eq 0 ]]; then
  log INFO "Monitoring VM run finished successfully. Full log: $LOG_FILE"
else
  log ERROR "Monitoring VM run FAILED (exit code $rc). Full log: $LOG_FILE"
fi
exit "$rc"
