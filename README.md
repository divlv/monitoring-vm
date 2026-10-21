# monitoring-vm

Ansible configuration of the **Monitoring VM**: a small Ubuntu 26.04 server (DigitalOcean, 2 GB RAM / 1 vCPU; two Kumas did not fit in 1 GB, see [ADR 0001](docs/adr/0001-kuma-in-k3s-on-a-1gb-vm.md)) that runs single-node K3s with one Uptime Kuma per Status site (domain), checks the Targets of two Groups (`MyMonit`, `ICFMonit`) every 30 s and alerts to Telegram. Each Group is visible only on its own domain. Vocabulary: [GLOSSARY.md](GLOSSARY.md). Decisions: [docs/adr](docs/adr).

Ansible runs **locally on the server, as root**. This repository is public: real Targets, domain, credentials and the Trusted IPs live only in the *Private config* on the server, never in git.

## Run

```bash
# 1. on the new server, as root
git clone https://github.com/divlv/monitoring-vm /opt/monitoring-vm

# 2. the Private config: start from config.sample.yml, replace every CHANGE_ME
install -d -m 0700 /etc/monitoring-vm
install -m 0600 /path/to/your/config.yml /etc/monitoring-vm/config.yml

# 3. configure the server (extra arguments go to ansible-playbook)
bash /opt/monitoring-vm/run_ansible.sh              # or ./run_ansible.sh after: git add --chmod=+x run_ansible.sh
bash /opt/monitoring-vm/run_ansible.sh --check --diff   # dry run
bash /opt/monitoring-vm/run_ansible.sh --tags k3s,kuma  # only a part: dns, base, tune, k3s, kuma, provision, firewall
```

`run_ansible.sh` installs `ansible-core` from apt if needed, gives the run a RunId and writes `/var/log/monitoring-vm/run_<RunId>.log` (every line has a timestamp and the RunId). It is safe to run repeatedly.

## What it does

1. **Preflight**: root, Ubuntu 26.04, a complete Private config, a non-empty Trusted IPs list.
2. **Google DNS** through systemd-resolved (provider DNS replaced per interface; tested, rolled back on failure).
3. **Base**: timezone Europe/Riga, packages, aliases, `autoexec.bat` + cron `@reboot`, security-only `unattended-upgrades` (never reboots by itself).
4. **1 GB tuning**: 1 GB swap file, `vm.swappiness=10`, journald 100 MB, snapd masked.
5. **K3s** (pinned version, `metrics-server` off) with Traefik: Let's Encrypt HTTP-01, **staging first** (`acme_use_staging: false` in the config, then re-run, for a trusted certificate).
6. **One Uptime Kuma per Status site** (its own namespace, data directory and ingress for its domain; the `sites:` block of the Private config assigns the Groups), then `ansible/files/kuma/provision_kuma.py` creates, in each, the same admin, the Telegram channel, that site's Groups and Targets. It never deletes anything, unless you add `-e kuma_prune=true` to also delete monitors that no longer belong to the site (e.g. a Group moved to another domain).
7. **Firewall** (`iptables`/`ipset`, last): tcp 80/443 open to everyone, SSH and ping only from the Trusted IPs, the rest closed. It refuses to run if the IP of your SSH session is not trusted.

A reboot is never done automatically; the run ends with a notice when `/var/run/reboot-required` exists.

## Check afterwards

```bash
k3s kubectl get pods -A          # traefik and one uptime-kuma pod per site Running
resolvectl status                # 8.8.8.8 / 8.8.4.4
iptables -t mangle -S MVM_PRE ; iptables -S MVM_IN
tail /var/log/monitoring-vm/*.log
```
