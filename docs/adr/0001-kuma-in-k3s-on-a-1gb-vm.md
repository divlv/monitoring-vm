# Run Uptime Kuma inside single-node K3s on a 1 GB VM

Everything on the Monitoring VM runs under K3s (Traefik handles HTTPS with Let's Encrypt, as in the arck3s sample), including Uptime Kuma; there is no standalone Docker. The droplet has 1 GB RAM / 1 vCPU, below the 2 GB / 2 vCPU that the K3s documentation lists as the minimum for a server, so this is a conscious trade-off: consistency with the user's other K3s servers and tooling over headroom.

## Consequences

- RAM is tight. Mitigations: `metrics-server` is disabled, a 1 GB swap file with `vm.swappiness=10` (kubelet runs with swap on), and a ~384 MiB memory limit on the Kuma pod. If it still starves, the agreed fallback is a 2 GB droplet, not removing K3s.
- A plain `docker run` of Kuma would be lighter, so do not "simplify" to it without reopening this decision.
- Kuma and Traefik data live on hostPath volumes (`/opt/kuma-data`, `/opt/traefik-data`), as in the arck3s sample.
