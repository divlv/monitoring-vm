# Run Uptime Kuma inside single-node K3s on a 1 GB VM

Everything on the Monitoring VM runs under K3s (Traefik handles HTTPS with Let's Encrypt, as in the arck3s sample), including Uptime Kuma; there is no standalone Docker. The droplet has 1 GB RAM / 1 vCPU, below the 2 GB / 2 vCPU that the K3s documentation lists as the minimum for a server, so this is a conscious trade-off: consistency with the user's other K3s servers and tooling over headroom.

## Consequences

- RAM is tight. Mitigations: `metrics-server` is disabled, a 1 GB swap file with `vm.swappiness=10` (kubelet runs with swap on), and a ~384 MiB memory limit on the Kuma pod. If it still starves, the agreed fallback is a 2 GB droplet, not removing K3s.
- **Outcome (2026-10-21):** one Kuma fitted on 1 GB (swap ~600 MB). When the Groups were split over two domains, i.e. a second Kuma, the box thrashed (swap 700-800 MB, ~20 MB/s swapped in and out, IO wait 50-60 %) and HTTP checks started to time out. The droplet was resized to 2 GB (CPU and RAM only, 1 vCPU); with it both Kumas run with ~100 MB of swap and no swap traffic. The 1 GB sizing above is therefore only valid for a single Kuma, and the file name is kept for history.
- A plain `docker run` of Kuma would be lighter, so do not "simplify" to it without reopening this decision.
- Kuma and Traefik data live on hostPath volumes (`/opt/kuma-data`, `/opt/traefik-data`), as in the arck3s sample.
