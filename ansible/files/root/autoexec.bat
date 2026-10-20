#!/bin/bash
#
# AUTOEXEC.BAT :-) -=- Started by cron (@reboot) each time the server boots.
#
# The firewall is not persisted by the system (the iptables-nft rules are lost on
# reboot), so it is applied again from the Trusted IPs file on every boot.
#
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mkdir -p /var/log/monitoring-vm
/root/iptables.sh >> /var/log/monitoring-vm/autoexec.log 2>&1
