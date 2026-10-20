# Firewall rules live in mangle PREROUTING, not only in INPUT

The host firewall (`ansible/files/root/iptables.sh`) drops every NEW inbound connection except tcp 80/443 and SSH/ping from the Trusted IPs, and it does so in the `mangle` table's PREROUTING chain, in addition to a default-DROP INPUT chain. The arck3s sample filters in INPUT only, which does not work for K3s: Traefik is published by DNAT to a pod (ServiceLB/portmap) and, through NodePorts 30000-32767, on the public IP as well; such packets take the FORWARD path, where flannel's rules accept them, and never reach INPUT. `mangle PREROUTING` runs after connection tracking but before the `nat` DNAT, so it sees the original destination port of everything.

## Considered Options

- **INPUT only (arck3s sample):** leaves the ingress and every NodePort open to the world regardless of the rules.
- **Filtering in FORWARD (`-I` before `FLANNEL-FWD`):** works for forwarded traffic but needs K3s-specific ordering that K3s can undo when it re-syncs its rules.
- **Traefik `ipAllowList`:** the real client IP at Traefik is unreliable behind ServiceLB (it can see the svclb pod address), and it would not cover NodePorts.

## Consequences

- Only the chains `MVM_PRE` (mangle) and `MVM_IN` (filter) are managed; K3s' own chains are never touched.
- The rules are not persisted by the system: `/root/autoexec.bat` (cron `@reboot`) re-applies them, so there is a short window after boot before they are in place.
- The script refuses to change anything when the SSH client's IP is not in the Trusted IPs (lockout protection).
