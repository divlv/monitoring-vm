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
- Verified on the server since: Ansible run, uptime-kuma-api2 2.9.0 with Kuma 2.5.6, DNS override on DigitalOcean, K3s/Traefik on 1 GB, Let's Encrypt issuance (staging). STILL unverified: the firewall on real iptables-nft, memory over days.

## Done
- Grilling session (Q1-Q25) finished and user confirmed; code, ADRs, README written; static checks done.
- Server `icfmonitor` is configured end to end EXCEPT the firewall step (held for the user's go): preflight, Google DNS (global + per-link, verified Google-only), base, 1 GB tuning, K3s v1.36.5+k3s1, Traefik (Let's Encrypt STAGING certificate issued for the UI domain over HTTP-01; http -> https 301), Kuma 2.5.6 pod, provisioning (RunId smb89hx: created=21 updated=1 = admin, settings, Telegram channel, 2 Groups, 17 Targets); a second dry run gives changed=0 (idempotent). Run 1 failed on a timeout (Traefik wait), runs 2-3 on a Kuma greeting race; both fixed (commits 517a752, f41180b, a67a2b3).
- Root cause of the provisioning failure (PROVEN with a verbose Socket.IO probe on the server): the client emits `needSetup` ~8 ms before Kuma finished `await sendInfo` and registered its handlers, so the call is dropped unanswered. The pre-login `info` event has no `version` and uptime-kuma-api2 ignores it, so waiting for `api.info()` can never work. Fix (a67a2b3) = 2 s pause + `need_setup` retries (5 x 5 s); the real dry run then answered on the first attempt.
- SSH access for Claude: alias `icfmonitor-root` (dedicated key ~/.ssh/id_claude_icfmonitor, host key verified by fingerprint). Revoke: delete the config block + key files + the "claude-icfmonitor" line in /root/.ssh/authorized_keys.

## Now
- Kuma monitors 17 Targets; its log shows 3 genuinely failing: the GWT-based MyMonit Target (HTTP 200 but marker `Main.nocache.js` not in the response body -> semantics of the old check_status.sh marker probably differ), a MyMonit demo Target (ECONNREFUSED on :443 from the new IP), the MyMonit Target on port 1080 (timeout; that port is probably restricted to trusted IPs). The new VM's public IP likely has to be allowed on those hosts' firewalls.
- BLOCKER for alerts: Telegram answers HTTP 400 "Bad Request: chat not found" -> `telegram.chat_id` in the Private config is wrong, or the bot is not in that chat / was never started. The user must correct it (I do not read that file), then `run_ansible.sh --tags provision` updates the channel.
- Waiting for the user: (1) go/no-go for the firewall step, (2) correct chat_id, (3) decision on the 3 failing Targets, (4) when satisfied set `acme_use_staging: false` in the Private config and re-run.
- Server memory after provisioning: used ~395 MB, swap 618/1023 MB (stable).

## Next
- Firewall: `ssh icfmonitor-root 'cd /opt/monitoring-vm && bash run_ansible.sh --tags firewall'` only after the user's OK (lockout check uses this machine's SSH IP, which the user says is in trusted_ips); verify with a NEW ssh session, `iptables -S MVM_IN`, `iptables -t mangle -S MVM_PRE`, 80/443 answering.
- After Telegram works: confirm one real alert arrives (a failing Target should notify).
- Un-comment the 8 ICFMonit targets once reachable and give them literal markers. Possible follow-up: `--status` mode for provision_kuma.py (list Targets with last heartbeat).
- Remind the user to remove the claude-icfmonitor key from /root/.ssh/authorized_keys when done.

## Open questions (UNCONFIRMED)
- Correct Telegram chat_id / is the bot in the chat.
- Semantics of the old `check_status.sh` marker for the GWT-based MyMonit Target.
- Are the demo Target and the port-1080 Target reachable from the new IP (their firewalls)? Are the 8 non-resolving ICFMonit hosts reachable from the internet?

## Working set (files/ids/commands)
- Run on server: `bash /opt/monitoring-vm/run_ansible.sh [--check --diff] [--tags dns,base,tune,k3s,kuma,provision,firewall]`
- Script dry run: `/opt/monitoring-vm-venv/bin/python -I ansible/files/kuma/provision_kuma.py --check --url http://<kuma-clusterip>:3001 --config /etc/monitoring-vm/config.yml`
- Logs on server: `/var/log/monitoring-vm/{run_*.log,kuma_provision_*.log,firewall.log,autoexec.log}`
- Repo: https://github.com/divlv/monitoring-vm (branch main)
