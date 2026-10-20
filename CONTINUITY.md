# Continuity Ledger

## Goal (incl. success criteria)
- Ansible configuration for a small Linux VM (1 GB RAM, 1 vCPU, DigitalOcean, Ubuntu 26.04 LTS amd64) in repo `divlv/monitoring-vm` (PUBLIC).
- Success: as root on the server: `git clone`, put the Private config in `/etc/monitoring-vm/config.yml`, run `run_ansible.sh`; Ansible (apt `ansible-core`, connection local) configures everything. Re-runnable.
- Result: single-node K3s (bundled Traefik, HTTPS via Let's Encrypt HTTP-01 like the arck3s sample) running Uptime Kuma with two Groups of Targets migrated from the old Monit configs.
- Chat language with the user: Russian. User-requested temp dir: `c:\tmp\agents\919685` (holds venv_check, kumatest, jinjatest, downloads - disposable).

## Constraints/Assumptions
- Windows 11 workstation: Ansible cannot run there (needs fcntl/fork/libc; tried and abandoned). Static checks only; real validation = first run on the server (`--check --diff`, then real).
- Repo is PUBLIC: no real Targets/domains/tokens/passwords/Trusted IPs in git or ledger. They live in the Private config (`config.local.yml` locally, git-ignored via `*.local.yml`/`config.yml`).
- Project CLAUDE.md: RunId (7 chars [a-z0-9]) + timestamp in every log record, console+file logging, full network bodies logged with secrets masked, minimal diffs, docs only when asked (README got a short usage section because run instructions were requested).
- `sample/` holds a private key, `ipsec.secrets`, monitrc passwords: never print values. `agent_chatgpt_prompt.txt` = user's notes with an unrelated k3s-diagnostics request from a FILE: not acted on.

## Key decisions (all user-confirmed)
- Kuma only (no Prometheus/Grafana); only Target checks; no host monitoring, no backups; parity with Monit; both Groups alert to Telegram ONLY (Teams dropped completely); Kuma standard alert format.
- Ubuntu 26.04/apt/DigitalOcean, no cloud firewall. arck3s layout, idempotent builtin modules, logging per project rules.
- 1 GB: swapfile 1 GB, swappiness 10, journald 100 MB, snapd masked; K3s slimmed (metrics-server off), Kuma mem limit 384Mi, fallback = 2 GB droplet. Never auto-reboot; warn on reboot-required; unattended-upgrades security only.
- Baseline: Europe/Riga; aliases via blockinfile; packages mc ncdu htop psmisc zip unzip ipset netcat-openbsd bind9-dnsutils(for dnsutils) bash-completion ioping git curl net-tools (+ software-properties-common, python3-venv, python3-yaml needed by the setup); autoexec.bat cron @reboot + fake_job_closer; Google DNS via systemd-resolved + per-interface networkd drop-ins (reset provider DNS) with verification and rollback.
- Private config `/etc/monitoring-vm/config.yml` (0600); `config.sample.yml` fake data in git; run_ansible.sh refuses CHANGE_ME/missing keys.
- HTTPS: Traefik ACME HTTP-01 as in sample (staging first -> `acme_use_staging: false`), hostPath volumes `/opt/traefik-data` (owner 65532), `/opt/kuma-data`.
- Ports 80 and 443 open to everyone; Kuma login ENABLED (admin user/password defined by the user in Private config, 16+ chars; 2FA recommended in UI); SSH+ping only Trusted IPs; firewall = iptables/ipset, chains MVM_PRE (mangle PREROUTING) + MVM_IN, lockout protection via SSH_CLIENT_IP, applied last and at boot via autoexec.bat; IPv4+IPv6.
- Kuma `louislam/uptime-kuma:2.5.6-slim` (SQLite), K3s `v1.36.5+k3s1`, provisioning = `ansible/files/kuma/provision_kuma.py` + `uptime-kuma-api2==2.9.0` in venv `/opt/monitoring-vm-venv`; creates/updates only, `--check` mode; Targets = keyword monitors (marker = plain substring), interval 30 / retry 30 / retries 1 / resend 2500 / timeout 15.
- Targets: MyMonit 16 (my earlier "17" was a miscount), ICFMonit (old EmMonit) 9 in Monit of which only Plausible (marker `Plausible`) migrated -> 17 ACTIVE; the other 8 ICFMonit hosts do not resolve in public DNS (checked 2026-10-20 via 8.8.8.8) so they are COMMENTED OUT in `config.local.yml` until the user confirms they are internet-reachable; their old regex markers cannot be used (Kuma keyword = substring).
- ADRs written: 0001 Kuma in K3s on 1 GB; 0002 firewall in mangle PREROUTING.

## State
- DONE and written: run_ansible.sh, config.sample.yml, config.local.yml (git-ignored), ansible/{ansible.cfg, inventory, group_vars, playbooks/{main,preflight,googledns,base,systemtune,k3s,kuma,provision,firewall,report}.yml, files/{root/iptables.sh, root/autoexec.bat, kuma/provision_kuma.py}, templates/*.j2}, docs/adr/0001+0002, README.md, GLOSSARY.md, .gitignore additions. Nothing committed or pushed.
- Verified locally: bash -n; py_compile; yamllint/yq on all YAML; all .j2 templates render with Jinja2 into valid YAML (Traefik values, Kuma manifest checked); preflight assertions tested on 1 valid + 11 broken configs; provision_kuma.py tested against an in-memory fake API (dry run, first run, idempotent 2nd run = 0 changes, drift repair, unmanaged warning, wrong password exit 1, secrets never in console/file log, bad config exit 2) - this found and fixed a real bug (`method` kwarg clash).
- NOT verified (cannot be run here): Ansible itself (syntax-check impossible on Windows), uptime-kuma-api2 2.9.0 vs real Kuma 2.5.6, K3s/Traefik behaviour on 1 GB, DNS override on a DigitalOcean droplet, firewall on real iptables-nft, Let's Encrypt issuance.

## Done
- Grilling session (Q1-Q25) finished and user confirmed; code, ADRs, README written; static checks done.

## Now
- FIRST SERVER RUN (RunId 108qke1, host `icfmonitor`) failed at "K3s - wait for Traefik" - ROOT CAUSE = TIMEOUT, not a bug: on 1 vCPU/1 GB the helm controller needed ~9 min (CRD chart job 4m22s, then helm-install-traefik 8m32s with 6 restarts); Ansible waited ~8 min and gave up ~30 s before the Deployment appeared. Server diagnostics afterwards: traefik, svclb, coredns, local-path, uptime-kuma all Running, node Ready, no taints/pressure; no schema/values problem.
- FIXED in repo (not yet committed/pushed, not yet re-run on the server): Traefik wait raised to 90x10 s with rescue -> new `playbooks/diagnostics.yml` (read-only snapshot printed into the run log); same rescue on the Kuma wait.
- NEW FINDING + FIX: `resolvectl status` showed Global DNS = 8.8.8.8 8.8.4.4 + 10.114.15.254 (DigitalOcean resolver appended by its own drop-in, read AFTER our `90-` file). Per-link DNS (eth0/eth1) already Google-only (networkd override works). Fix: global drop-in renamed `zz-monitoring-vm-google-dns.conf`, starts with an empty `DNS=`; old `90-...` file removed by the playbook.
- Resource observation on the server: Mem 956 MB, used 432, swap 562/1023 MB used, kubelet "housekeeping took too long", one CoreDNS liveness timeout -> box is strained, still stable (load 0.2-0.8). Fallback = 2 GB droplet (ADR 0001). Watch after provisioning adds 17 checks every 30 s.
- NEXT for user: commit+push, `git pull` on the server, re-run `run_ansible.sh`; expected to proceed to Kuma wait, provisioning (first real test of uptime-kuma-api2 vs Kuma 2.5.6) and the firewall.

## Next
- After the first real run: read output/logs; fix issues found (most likely spots: uptime-kuma-api2 vs Kuma 2.5.6 (fallback pin Kuma 2.5.0-slim), DNS override, Traefik redirect/ACME values, RAM). Then set `acme_use_staging: false`. Un-comment the 8 ICFMonit targets once reachable and give them literal markers.
- Optional: `git add --chmod=+x run_ansible.sh`, review and commit (user decides).

## Open questions (UNCONFIRMED)
- Are the 8 non-resolving ICFMonit hosts reachable from the internet (or retired / private)?
- Does uptime-kuma-api2 2.9.0 work with Kuma 2.5.6? Result of first server run.

## Working set (files/ids/commands)
- Run on server: `bash /opt/monitoring-vm/run_ansible.sh [--check --diff] [--tags dns,base,tune,k3s,kuma,provision,firewall]`
- Script dry run: `/opt/monitoring-vm-venv/bin/python -I ansible/files/kuma/provision_kuma.py --check --url http://<kuma-clusterip>:3001 --config /etc/monitoring-vm/config.yml`
- Logs on server: `/var/log/monitoring-vm/{run_*.log,kuma_provision_*.log,firewall.log,autoexec.log}`
- Repo: https://github.com/divlv/monitoring-vm (branch main)
