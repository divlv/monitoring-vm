#!/usr/bin/env bash
#
# Host firewall of the Monitoring VM (IPv4 + IPv6, iptables + ipset). There is no cloud
# firewall, this is the only one. Re-runnable; started by Ansible and again at every
# boot by /root/autoexec.bat (cron @reboot), the system does not persist the rules.
#
# Policy
#   * tcp 80/443 (Traefik: Kuma UI and the Let's Encrypt HTTP-01 challenge) - from anyone;
#   * SSH (22) and ping - only from the Trusted IPs (/etc/monitoring-vm/trusted_ips);
#   * everything else NEW from outside - dropped; replies to our own traffic pass.
#
# Why "mangle PREROUTING" and not just INPUT: K3s publishes Traefik by DNAT to a pod
# (ServiceLB/portmap) and, via NodePorts, on the public IP too. Such packets take the
# FORWARD path and never see INPUT, so INPUT rules alone would not restrict them.
# PREROUTING in the mangle table runs after connection tracking but BEFORE the DNAT of
# the nat table, so it sees the original destination port of every packet. See
# docs/adr/0002-firewall-in-mangle-prerouting.md.
#
# Only the chains MVM_PRE (mangle) and MVM_IN (filter) are managed; K3s' own chains
# (FORWARD, nat, KUBE-*, FLANNEL-*) are never touched.
#
# Lockout protection: with SSH_CLIENT_IP set (run_ansible.sh does it) nothing is changed
# unless that address is in the Trusted IPs.
#
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

TRUSTED_FILE="${TRUSTED_FILE:-/etc/monitoring-vm/trusted_ips}"
LOG_DIR=/var/log/monitoring-vm
LOG_FILE="$LOG_DIR/firewall.log"

gen_run_id() {
  local chars=abcdefghijklmnopqrstuvwxyz0123456789 id="" i
  for i in 1 2 3 4 5 6 7; do
    id+="${chars:RANDOM%36:1}"
  done
  printf '%s' "$id"
}
RUN_ID="${RUN_ID:-$(gen_run_id)}"

mkdir -p "$LOG_DIR"
log() {
  local level="$1"
  shift
  printf '%(%F %T)T %s RunId: %s %s\n' -1 "$level" "$RUN_ID" "$*" | tee -a "$LOG_FILE"
}
die() {
  log ERROR "$*"
  exit 1
}

[[ $EUID -eq 0 ]] || die "must run as root"
for tool in iptables ip6tables ipset modprobe; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done
[[ -r "$TRUSTED_FILE" ]] || die "Trusted IPs file $TRUSTED_FILE not found"

modprobe ip_set || die "kernel module ip_set is not available"
modprobe ip_set_hash_net || die "kernel module ip_set_hash_net is not available"

# --- Trusted IPs -------------------------------------------------------------------------
TRUSTED4=()
TRUSTED6=()
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  line="${line//[[:space:]]/}"
  [[ -z "$line" ]] && continue
  if [[ "$line" == *:* ]]; then
    TRUSTED6+=("$line")
  else
    TRUSTED4+=("$line")
  fi
done < "$TRUSTED_FILE"
(( ${#TRUSTED4[@]} + ${#TRUSTED6[@]} > 0 )) || die "Trusted IPs file $TRUSTED_FILE is empty - refusing to continue"
log INFO "Trusted IPv4 entries: ${#TRUSTED4[@]}, IPv6 entries: ${#TRUSTED6[@]}"

# Build "<name>_new" next to the live set; swapped in only after the lockout check.
stage_set() {
  local name="$1" family="$2"
  shift 2
  ipset create -exist "$name" hash:net family "$family"
  ipset create -exist "${name}_new" hash:net family "$family"
  ipset flush "${name}_new"
  local entry
  for entry in "$@"; do
    ipset add -exist "${name}_new" "$entry" || die "invalid trusted entry '$entry'"
  done
}
swap_set() {
  local name="$1"
  ipset swap "${name}_new" "$name"
  ipset destroy "${name}_new"
}

stage_set mvm_trusted4 inet "${TRUSTED4[@]}"
stage_set mvm_trusted6 inet6 "${TRUSTED6[@]}"

if [[ -n "${SSH_CLIENT_IP:-}" ]]; then
  if [[ "$SSH_CLIENT_IP" == *:* ]]; then
    check_set=mvm_trusted6_new
  else
    check_set=mvm_trusted4_new
  fi
  if ipset test "$check_set" "$SSH_CLIENT_IP" 2>/dev/null; then
    log INFO "Lockout check passed: SSH client $SSH_CLIENT_IP is a trusted IP"
  else
    ipset destroy mvm_trusted4_new 2>/dev/null || true
    ipset destroy mvm_trusted6_new 2>/dev/null || true
    die "SSH client $SSH_CLIENT_IP is NOT in $TRUSTED_FILE - firewall NOT changed (you would lock yourself out)"
  fi
else
  log WARNING "SSH_CLIENT_IP is not set - lockout check skipped"
fi

swap_set mvm_trusted4
swap_set mvm_trusted6

# --- Physical network interfaces (public, and the VPC one if any) ----------------------
NICS=()
for path in /sys/class/net/*; do
  nic="${path##*/}"
  [[ "$nic" == lo ]] && continue
  [[ -e "$path/device" ]] && NICS+=("$nic")   # veth, bridges and VXLAN have no "device"
done
(( ${#NICS[@]} > 0 )) || die "no physical network interface found"
log INFO "Protected interfaces: ${NICS[*]}"

# --- Rules -------------------------------------------------------------------------------
# $1 = iptables|ip6tables   $2 = ipset name   $3 = 4|6
apply_family() {
  local ipt="$1" set="$2" fam="$3" rule nic t

  # mangle/PREROUTING: decides about every NEW packet before any DNAT happened.
  "$ipt" -t mangle -n -L MVM_PRE >/dev/null 2>&1 || "$ipt" -t mangle -N MVM_PRE
  "$ipt" -t mangle -F MVM_PRE
  "$ipt" -t mangle -A MVM_PRE -m conntrack --ctstate INVALID -j DROP
  "$ipt" -t mangle -A MVM_PRE -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  if [[ "$fam" == 6 ]]; then
    # Neighbour/router discovery is not connection-tracked and IPv6 does not work without it.
    for t in 133 134 135 136; do
      "$ipt" -t mangle -A MVM_PRE -p ipv6-icmp --icmpv6-type "$t" -j RETURN
    done
  fi
  "$ipt" -t mangle -A MVM_PRE -p tcp -m multiport --dports 80,443 -j RETURN
  "$ipt" -t mangle -A MVM_PRE -m set --match-set "$set" src -j RETURN
  "$ipt" -t mangle -A MVM_PRE -j DROP

  # (Re)hook it first in PREROUTING for the physical interfaces only: pod and loopback
  # traffic never goes through it.
  while IFS= read -r rule; do
    # shellcheck disable=SC2086
    "$ipt" -t mangle ${rule/-A/-D}
  done < <("$ipt" -t mangle -S PREROUTING | grep -- '-j MVM_PRE' || true)
  for nic in "${NICS[@]}"; do
    "$ipt" -t mangle -I PREROUTING 1 -i "$nic" -j MVM_PRE
  done

  # filter/INPUT: what may reach a process on this host.
  "$ipt" -n -L MVM_IN >/dev/null 2>&1 || "$ipt" -N MVM_IN
  "$ipt" -F MVM_IN
  "$ipt" -A MVM_IN -i lo -j ACCEPT
  "$ipt" -A MVM_IN -m conntrack --ctstate INVALID -j DROP
  "$ipt" -A MVM_IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  if [[ "$fam" == 6 ]]; then
    for t in 133 134 135 136; do
      "$ipt" -A MVM_IN -p ipv6-icmp --icmpv6-type "$t" -j ACCEPT
    done
  fi
  # Pods talk to the host (API server on 6443, kubelet) through these K3s interfaces.
  "$ipt" -A MVM_IN -i cni0 -j ACCEPT
  "$ipt" -A MVM_IN -i flannel.1 -j ACCEPT
  "$ipt" -A MVM_IN -p tcp -m multiport --dports 80,443 -j ACCEPT
  "$ipt" -A MVM_IN -p tcp --dport 22 -m set --match-set "$set" src -j ACCEPT
  if [[ "$fam" == 6 ]]; then
    "$ipt" -A MVM_IN -p ipv6-icmp -m set --match-set "$set" src -j ACCEPT
  else
    "$ipt" -A MVM_IN -p icmp -m set --match-set "$set" src -j ACCEPT
  fi
  "$ipt" -C INPUT -j MVM_IN 2>/dev/null || "$ipt" -I INPUT 1 -j MVM_IN
  "$ipt" -P INPUT DROP   # last, when everything that must pass is already in place

  log INFO "$ipt rules in place (mangle MVM_PRE, filter MVM_IN, INPUT policy DROP)"
  "$ipt" -t mangle -S MVM_PRE | while IFS= read -r rule; do log INFO "[$ipt] $rule"; done
  "$ipt" -S MVM_IN | while IFS= read -r rule; do log INFO "[$ipt] $rule"; done
}

apply_family iptables mvm_trusted4 4

if ip6tables -t mangle -n -L >/dev/null 2>&1; then
  apply_family ip6tables mvm_trusted6 6
else
  log WARNING "ip6tables is not usable on this kernel - IPv6 rules skipped (check that the server has no global IPv6 address)"
fi

log INFO "Firewall applied."
