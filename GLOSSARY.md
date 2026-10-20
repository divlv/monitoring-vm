# Monitoring VM

A single small virtual machine that continuously checks a fixed set of web endpoints and alerts people when one stops responding. It replaces an older Monit-based setup.

## Language

**Monitoring VM**:
The one virtual machine configured by this repository, which performs all checks and sends all alerts.
_Avoid_: monitor server, the server (ambiguous with Target)

**Target**:
A web endpoint (an HTTP(S) address) whose availability is checked.
_Avoid_: server, site, host, service

**Group**:
A named set of Targets that are watched and alerted together. The current Groups are **MyMonit** and **ICFMonit** (ICFMonit was previously called EmMonit).
_Avoid_: cluster, folder, project

**Health marker**:
A string that a healthy Target is expected to return in its response; a Target counts as down when the marker is missing. Most Targets use the marker `#GTO` embedded in their page.
_Avoid_: keyword, content check, signature

**Alert**:
A message sent to people when a Target goes down, and again when it recovers.
_Avoid_: notification, ping

**Trusted IPs**:
The list of source addresses that are allowed administrative access (SSH) to the Monitoring VM; the web UI is public and protected by login.
_Avoid_: whitelist, allow-list

**Private config**:
The file kept only on the Monitoring VM that holds everything not meant to be public: the real Targets, their Groups and Health markers, credentials, and the Trusted IPs. It is never committed to git.
_Avoid_: settings, secrets file
