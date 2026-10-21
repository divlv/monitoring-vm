# Plan: deploy and iterate on icfmonitor over SSH (root), finish the first full run

## Context
Server `icfmonitor` (DigitalOcean, Ubuntu 26.04, root, 1 GB / 1 vCPU) was configured by `run_ansible.sh`. Run 2 got through K3s, Traefik and the Kuma rollout but `provision_kuma.py` failed: Kuma 2.5.6 drops the client's first `needSetup` call (its handlers are registered after `await sendInfo`). The fix (wait for the `info` event, retry `need_setup`, diagnostics block/rescue) exists locally but is not committed, so it is not on the server. The user allowed me to connect as root and finish the work myself.

Decisions from the user: dedicated new SSH key for me; I commit and push to `main`; autonomy = read + run the playbook, ask before the firewall step, any reboot and any manual change outside Ansible; this Windows machine connects and its public IP is already in `trusted_ips`.

## Step A - access (local only, nothing on the server yet)
1. `ssh-keygen -t ed25519 -N "" -C claude-icfmonitor -f ~/.ssh/id_claude_icfmonitor` (new files only; the private key is never read or printed).
2. Append to `~/.ssh/config` an alias `icfmonitor-root`: `HostName 207.154.194.247`, `User root`, `IdentityFile ~/.ssh/id_claude_icfmonitor`, `IdentitiesOnly yes`, `BatchMode yes`, `ConnectTimeout 10`, `ServerAliveInterval 30`, `ClearAllForwardings yes`, `ForwardAgent no`. Host-key checking stays on (check `ssh-keygen -F 207.154.194.247`; if missing, the user connects once with their own key to record it).
3. Give the user the public key and ONE command to run in their own session: append it to `/root/.ssh/authorized_keys` with `no-port-forwarding,no-agent-forwarding,no-X11-forwarding`.
4. Test: `ssh icfmonitor-root 'id -un; hostname; uptime'`.

## Step B - ship the fix (explicitly allowed)
Stage explicit paths only (never `git add -A`): `.gitignore`, `README.md`, `CONTINUITY.md`, `GLOSSARY.md`, `config.sample.yml`, `run_ansible.sh` (`git add --chmod=+x`), `ansible/`, `docs/`. Not staged: `config.local.yml` (ignored), `.claude_plans/`, `sample/`. Before committing: grep the staged diff for the real domain, tokens and the server IP; show `git diff --cached --stat`. Commit with the attribution trailer, `git push origin main`.

## Step C - run on the server (firewall deliberately skipped)
1. `ssh icfmonitor-root 'cd /opt/monitoring-vm && git pull --ff-only && git log -1 --oneline'`.
2. Start detached so a dropped SSH session cannot kill it: `nohup setsid bash run_ansible.sh --skip-tags firewall > /var/log/monitoring-vm/last_run.out 2>&1 &`; poll with short read-only `tail`/`pgrep` calls (run log `/var/log/monitoring-vm/run_<RunId>.log`).
3. On failure: read the tail of `kuma_provision_<RunId>.log` and `k3s kubectl -n uptime-kuma logs deployment/uptime-kuma --tail=80` (the playbook's rescue prints them too), fix locally, commit, push, pull, re-run. Loop until green.

## Step D - verify provisioning
- Run output ends with `RESULT created=...`; a second run reports `changed=0` (idempotent).
- Read-only: `/opt/monitoring-vm-venv/bin/python -I /opt/monitoring-vm/ansible/files/kuma/provision_kuma.py --check --url http://<kuma-clusterip>:3001 --config /etc/monitoring-vm/config.yml` -> `changed=0` (the script reads the config itself; I never open it).
- From this machine: `curl -I http://status...` -> 301 to https; `curl -sk -o NUL -w "%{http_code}" https://status...` -> 200 (staging certificate, hence `-k`); Traefik logs show the ACME order.
- `free -m` / `swapon --show` after provisioning (1 GB check).

## Step E - firewall (ASK FIRST, then one separate run)
Only after the user says go (optionally after a DigitalOcean snapshot): `ssh icfmonitor-root 'cd /opt/monitoring-vm && bash run_ansible.sh --tags firewall'`. `SSH_CONNECTION` carries this machine's IP, so the lockout check protects us; if it refuses, report instead of working around. Afterwards: open a NEW ssh session to prove access, check `iptables -S MVM_IN`, `iptables -t mangle -S MVM_PRE`, and that 80/443 still answer. Emergency path: DigitalOcean Droplet Console.

## Step F - close
Update `CONTINUITY.md`, commit and push it. Remind the user to delete my key line from `/root/.ssh/authorized_keys` (and optionally the local key and alias).

## Boundaries (always)
Never read `/etc/monitoring-vm/config.yml` or the Trusted IPs file; never print secrets; no reboot, no manual edits on the server outside Ansible, no sshd/authorized_keys changes by me, no DigitalOcean API; use only the alias `icfmonitor-root`, no scp/forwarding/TTY; never disable host-key checking. Claude Code's own permission prompts for `ssh` calls stay as configured (I do not touch `settings.json`).

## Critical files
`run_ansible.sh`, `ansible/playbooks/*.yml`, `ansible/files/kuma/provision_kuma.py`, `ansible/files/root/iptables.sh`; local `~/.ssh/config` and the new key.
