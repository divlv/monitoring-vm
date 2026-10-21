#!/usr/bin/env python3
"""Create or update Uptime Kuma Groups, Targets and the Telegram alert channel.

Reads the Private config of the Monitoring VM and brings a running Uptime Kuma in line
with it through Kuma's socket.io API (the uptime-kuma-api2 client). It is safe to run
repeatedly: whatever already matches is left alone, and nothing is ever deleted (Kuma
monitors that are not in the config are only reported as unmanaged).

What is provisioned:
    * the administrator (only on a fresh Kuma, via the one-time "setup" call);
    * the settings "Primary Base URL" and "Trust Proxy" (Kuma sits behind Traefik);
    * one Telegram notification channel;
    * one Group monitor per config group and one keyword monitor per Target, parameters
      migrated from Monit: check every 30 s, DOWN after 2 failed checks, alert repeated
      every 2500 checks. A Target is UP when HTTP status is 2xx and the response contains
      its Health marker (a plain, case-sensitive substring).

The alert channel is attached to the Targets only, never to the Groups: a Group goes DOWN
when any child does, so attaching it there too would send every alert twice.

Sites: the config may define several ``sites``, each with its own domain and its own
Groups; every site is a separate Kuma, so a site's UI shows nothing of the others. This
script provisions ONE Kuma per call (``--site ID`` picks which Groups and domain apply).
The ``sites`` block is mandatory: a config without it is rejected (exit 2) instead of silently
merging every Group into one Kuma.

Pruning: by default nothing is ever deleted. ``--prune`` additionally deletes the Kuma
monitors that the config does not describe for this site (children first, then their
Groups), e.g. the Groups that were moved to another site. Use ``--check --prune`` first.

Secrets (Kuma admin password, Telegram bot token) are read from the config file here, so
they never pass through Ansible variables or command lines, and they are masked as ***
in every log record. The console shows INFO; the log file additionally holds DEBUG with
the full body of every API request and response (secrets masked).

Examples:
    Dry run against Kuma on its cluster IP, from the provisioning virtualenv:

        $ /opt/monitoring-vm-venv/bin/python -I provision_kuma.py --check \\
              --url http://10.43.12.34:3001 --config /etc/monitoring-vm/config.yml

Exit codes:
    0  success (also for a dry run)
    1  Kuma could not be reached, refused a call, or the admin login failed
    2  the config file is missing or invalid

Note:
    uptime-kuma-api2 2.9.0 is tested up to Kuma 2.5.0; this runs against 2.5.6, so the
    first run on a real server is the compatibility test - read its output.
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import yaml
from uptime_kuma_api import MonitorType, NotificationType, Timeout, UptimeKumaApi, UptimeKumaException

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2

LOG = logging.getLogger("kuma_provision")

# Same values the Monit setup used; every key may be overridden in the config under
# "monitoring" or per Target.
DEFAULT_MONITORING = {
    "interval": 30,
    "retry_interval": 30,
    "retries": 1,
    "resend_interval": 2500,
    "timeout": 15,
}

# Fields compared with what Kuma already has; a difference triggers an edit.
COMPARED_FIELDS = (
    "type", "parent", "url", "keyword", "method", "interval", "retryInterval",
    "maxretries", "resendInterval", "timeout", "maxredirects", "accepted_statuscodes",
)

SENSITIVE_KEY_PARTS = ("password", "token", "secret", "authorization", "webhook")

# Seconds to let Kuma finish its connection greeting before the first call (see Provisioner.let_server_greet).
GREETING_DELAY = 2.0


class ConfigError(Exception):
    """The Private config is missing, unreadable or invalid."""


class ProvisionError(Exception):
    """Kuma could not be reached or refused a call."""


@dataclass(frozen=True)
class TargetSpec:
    """One Target as it must exist in Kuma.

    Attributes:
        group: Name of the Group it belongs to.
        name: Monitor name in Kuma (unique within its Group).
        url: Address that is requested with GET.
        marker: Health marker, a plain case-sensitive substring of the response.
        interval: Seconds between checks.
        retry_interval: Seconds between retries after a failed check.
        retries: Failed checks tolerated before the Target is DOWN.
        resend_interval: The DOWN alert is repeated every this many checks.
        timeout: Request timeout in seconds.
    """

    group: str
    name: str
    url: str
    marker: str
    interval: int
    retry_interval: int
    retries: int
    resend_interval: int
    timeout: int


@dataclass(frozen=True)
class ProvisionConfig:
    """The validated, normalised part of the Private config this script needs.

    Attributes:
        site: Id of the site this Kuma serves.
        domain: Public host name of the Kuma UI of this site.
        admin_user: Kuma administrator login.
        admin_password: Kuma administrator password.
        telegram_token: Telegram bot token.
        telegram_chat_id: Telegram chat the alerts go to.
        notification_name: Name of the notification channel inside Kuma.
        groups: Group names of this site in config order, without duplicates.
        targets: The Targets of this site's Groups in config order.
    """

    site: str
    domain: str
    admin_user: str
    admin_password: str
    telegram_token: str
    telegram_chat_id: str
    notification_name: str
    groups: list[str]
    targets: list[TargetSpec]

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in a log record."""
        return [self.admin_password, self.telegram_token]


class RunIdFilter(logging.Filter):
    """Stamps every log record with the RunId of this run."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self._run_id
        return True


class MaskingFormatter(logging.Formatter):
    """Formatter that replaces known secret values with ``***`` in the final text.

    The secrets are only known after the config was read, which in turn can fail and
    must be logged, hence :meth:`add_secrets` instead of a constructor argument.
    """

    def __init__(self, fmt: str) -> None:
        super().__init__(fmt)
        self._secrets: list[str] = []

    def add_secrets(self, secrets: list[str]) -> None:
        """Start masking the given values (longest first, so overlaps mask fully)."""
        merged = {s for s in [*self._secrets, *secrets] if s}
        self._secrets = sorted(merged, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def mask_sensitive(value: Any) -> Any:
    """Return a copy of ``value`` with the values of sensitive-looking keys replaced by ***.

    Complements :class:`MaskingFormatter`, which only knows the exact secrets of the
    config: this also hides e.g. a token Kuma returns for a key we never saw.

    Args:
        value: Any JSON-like structure (dicts, lists, scalars).

    Returns:
        The same structure with masked values.
    """
    if isinstance(value, dict):
        masked = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS) and item not in (None, "", False):
                masked[key] = "***"
            else:
                masked[key] = mask_sensitive(item)
        return masked
    if isinstance(value, (list, tuple)):
        return [mask_sensitive(item) for item in value]
    return value


def setup_logging(run_id: str, log_file: str | None, level: str) -> MaskingFormatter:
    """Configure console (INFO and above) and file (DEBUG) logging.

    Every record carries a timestamp and ``RunId: <run_id>``.

    Args:
        run_id: Identifier shared by all log records of this run.
        log_file: Path of the log file, or None for console only.
        level: Console level name (DEBUG, INFO, WARNING, ERROR).

    Returns:
        The formatter, so the caller can register secrets to mask once the config is read.
    """
    formatter = MaskingFormatter("%(asctime)s %(levelname)s RunId: %(run_id)s %(message)s")
    run_filter = RunIdFilter(run_id)

    LOG.setLevel(logging.DEBUG)
    LOG.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level.upper())
    console.setFormatter(formatter)
    console.addFilter(run_filter)
    LOG.addHandler(console)

    if log_file:
        os.umask(0o077)  # the DEBUG file holds full API bodies: root only
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(run_filter)
        LOG.addHandler(file_handler)
    return formatter


def _require(raw: dict, *path: str) -> Any:
    """Return ``raw[path[0]][path[1]]...`` or raise :class:`ConfigError` naming the key."""
    node: Any = raw
    for key in path:
        if not isinstance(node, dict) or key not in node or node[key] in (None, ""):
            raise ConfigError(f"config key '{'.'.join(path)}' is missing or empty")
        node = node[key]
    return node


def _select_site(raw: dict, site_id: str | None) -> tuple[str, str, list[str]]:
    """Pick the site this call is about.

    Args:
        raw: The parsed config.
        site_id: Id given with ``--site``, or None.

    Returns:
        ``(site id, domain, names of its Groups)``.

    Raises:
        ConfigError: If there is no ``sites`` block, the site is unknown or ambiguous, or the sites
            do not partition the Groups.
    """
    group_names = [str(_require(group, "name")) for group in _require(raw, "groups")]
    if "sites" not in raw:
        # No silent single-Kuma fallback: a config that lost its "sites:" block would merge
        # every Group into one Kuma again (and --prune would not notice).
        raise ConfigError("config has no 'sites:' block (the old single 'domain:' key was replaced by it)")

    sites = raw["sites"]
    if not isinstance(sites, list) or not sites:
        raise ConfigError("config key 'sites' must be a non-empty list")
    ids = [str(_require(site, "id")) for site in sites]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate site ids in 'sites'")
    assigned = [str(name) for site in sites for name in (_require(site, "groups"))]
    if sorted(assigned) != sorted(group_names):
        raise ConfigError("every Group must belong to exactly one site: check the 'groups' lists in 'sites'")

    if site_id is None:
        if len(sites) != 1:
            raise ConfigError(f"the config defines {len(sites)} sites, choose one with --site ({', '.join(ids)})")
        site = sites[0]
    else:
        matching = [site for site in sites if str(site["id"]) == site_id]
        if not matching:
            raise ConfigError(f"unknown site '{site_id}', the config has: {', '.join(ids)}")
        site = matching[0]
    return str(site["id"]), str(_require(site, "domain")), [str(name) for name in site["groups"]]


def load_config(path: str, site_id: str | None = None) -> ProvisionConfig:
    """Read and validate the Private config for one site.

    Args:
        path: Path of the YAML config file.
        site_id: Which site's Groups and domain to load (see :func:`_select_site`).

    Returns:
        The normalised configuration of that site.

    Raises:
        ConfigError: If the file cannot be read or a required value is missing/invalid.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path} is not a YAML mapping")

    site, domain, site_groups = _select_site(raw, site_id)
    defaults = {**DEFAULT_MONITORING, **(raw.get("monitoring") or {})}
    groups: list[str] = []
    targets: list[TargetSpec] = []
    seen: set[tuple[str, str]] = set()

    for group in _require(raw, "groups"):
        group_name = _require(group, "name")
        if group_name not in site_groups:
            continue
        if group_name not in groups:
            groups.append(group_name)
        for target in group.get("targets") or []:
            name = _require(target, "name")
            if (group_name, name) in seen:
                raise ConfigError(f"duplicate Target '{name}' in group '{group_name}'")
            seen.add((group_name, name))
            url = str(_require(target, "url"))
            if not url.startswith(("http://", "https://")):
                raise ConfigError(f"Target '{name}': url must start with http:// or https://")
            params = {**defaults, **{k: target[k] for k in DEFAULT_MONITORING if k in target}}
            targets.append(TargetSpec(
                group=group_name,
                name=str(name),
                url=url,
                marker=str(_require(target, "marker")),
                interval=int(params["interval"]),
                retry_interval=int(params["retry_interval"]),
                retries=int(params["retries"]),
                resend_interval=int(params["resend_interval"]),
                timeout=int(params["timeout"]),
            ))

    if not targets:
        raise ConfigError(f"no Targets configured{f' for site {site!r}' if site else ''}")
    return ProvisionConfig(
        site=site,
        domain=domain,
        admin_user=str(_require(raw, "kuma", "admin_user")),
        admin_password=str(_require(raw, "kuma", "admin_password")),
        telegram_token=str(_require(raw, "telegram", "bot_token")),
        telegram_chat_id=str(_require(raw, "telegram", "chat_id")),
        notification_name=str((raw.get("telegram") or {}).get("name") or "Telegram"),
        groups=groups,
        targets=targets,
    )


def connect(url: str, timeout: float, attempts: int = 6, delay: float = 10.0) -> UptimeKumaApi:
    """Connect to Kuma, retrying: right after the pod became ready the socket may refuse.

    Args:
        url: Base URL of Kuma, e.g. ``http://10.43.12.34:3001``.
        timeout: Seconds to wait for each API answer.
        attempts: How many times to try before giving up.
        delay: Seconds between attempts.

    Returns:
        A connected API client (use it as a context manager to disconnect).

    Raises:
        ProvisionError: If every attempt failed.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        LOG.info("Connecting to Kuma at %s (attempt %d/%d)", url, attempt, attempts)
        try:
            return UptimeKumaApi(url, timeout=timeout)
        except Exception as exc:  # the client raises several unrelated socket.io/requests types
            last_error = exc
            LOG.warning("Connection failed: %s", exc)
            if attempt < attempts:
                time.sleep(delay)
    raise ProvisionError(f"cannot connect to Kuma at {url}: {last_error}")


class Provisioner:
    """Applies a :class:`ProvisionConfig` to a connected Kuma.

    Attributes:
        stats: Counter of ``created``, ``updated``, ``deleted`` and ``unchanged`` objects.
    """

    def __init__(self, api: UptimeKumaApi, config: ProvisionConfig, check_only: bool, prune: bool = False) -> None:
        """Create a provisioner.

        Args:
            api: Connected Kuma API client.
            config: What Kuma must look like afterwards.
            check_only: When True nothing is written, the plan is only logged.
            prune: When True the monitors that the config does not describe are deleted
                instead of only being reported.
        """
        self.api = api
        self.cfg = config
        self.check_only = check_only
        self.prune = prune
        self.stats: Counter[str] = Counter()
        self._notification_id: int = -1   # -1 = does not exist yet (only possible in check mode)
        self._group_ids: dict[str, int] = {}
        self._monitors: list[dict] = []

    def _call(self, method: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Call an API method, logging the full request and response at DEBUG (masked).

        ``method`` is positional-only because Kuma monitors have a field called ``method``
        (GET/POST), which arrives here as a keyword argument.
        """
        name = method.__name__
        request = {"args": mask_sensitive(list(args)), "kwargs": mask_sensitive(kwargs)}
        LOG.debug("API request: %s %s", name, json.dumps(request, default=str, ensure_ascii=False))
        try:
            response = method(*args, **kwargs)
        except Exception as exc:
            LOG.error("API call %s failed: %s %s", name, type(exc).__name__, exc)
            raise
        LOG.debug("API response: %s %s", name, json.dumps(mask_sensitive(response), default=str, ensure_ascii=False))
        return response

    def _call_with_retry(
        self, method: Any, /, *args: Any, attempts: int = 5, attempt_timeout: float = 5.0, **kwargs: Any
    ) -> Any:
        """Like :meth:`_call` for a harmless read, repeated when the answer never comes.

        Each attempt waits only ``attempt_timeout`` seconds (instead of the client's long
        default), so a dropped call costs seconds, not half a minute.

        Args:
            method: The API method to call.
            *args: Positional arguments for it.
            attempts: How many times to try before the Timeout is passed on.
            attempt_timeout: Seconds to wait for the answer of each attempt.
            **kwargs: Keyword arguments for it.

        Raises:
            Timeout: If every attempt timed out.
        """
        default_timeout = self.api.timeout
        self.api.timeout = min(default_timeout, attempt_timeout)
        try:
            for attempt in range(1, attempts + 1):
                try:
                    return self._call(method, *args, **kwargs)
                except Timeout:
                    if attempt == attempts:
                        raise
                    LOG.warning("%s got no answer (attempt %d/%d), trying again", method.__name__, attempt, attempts)
        finally:
            self.api.timeout = default_timeout

    def let_server_greet(self) -> None:
        """Give Kuma time to finish greeting this client before the first call.

        Kuma 2.x runs ``await sendInfo(socket)`` first in its connection handler and only
        THEN registers the handlers for ``needSetup``, ``login`` and the rest (server.js).
        The API client calls ``needSetup`` the moment it is connected; if that arrives
        before the handler exists, Socket.IO drops it silently and it is never answered
        (seen on the droplet: ``needSetup`` sent 8 ms before the server's ``info``/``setup``/
        ``loginRequired`` events, then a 20-30 s Timeout). The pre-login ``info`` event has
        no ``version`` and the client ignores it, so there is nothing to wait for; a short
        pause plus the retries of :meth:`_call_with_retry` cover the gap.
        """
        time.sleep(GREETING_DELAY)

    def _write(self, description: str, counter: str) -> bool:
        """Count a planned change and say whether it must really be executed."""
        self.stats[counter] += 1
        if self.check_only:
            LOG.info("[check] would %s", description)
            return False
        LOG.info("%s", description[0].upper() + description[1:])
        return True

    def login(self) -> bool:
        """Create the administrator on a fresh Kuma, then log in.

        Returns:
            False when this is a dry run and the administrator does not exist yet, so
            nothing else can be inspected; True otherwise.

        Raises:
            ProvisionError: If Kuma refuses the setup or the login.
        """
        user = self.cfg.admin_user
        if self._call_with_retry(self.api.need_setup):
            if not self._write(f"create the Kuma administrator '{user}' (first start)", "created"):
                LOG.info("[check] Kuma is not set up yet, so Groups and Targets cannot be inspected")
                return False
            try:
                self._call(self.api.setup, user, self.cfg.admin_password)
            except UptimeKumaException as exc:
                raise ProvisionError(f"Kuma refused to create the administrator (weak password?): {exc}") from exc
        try:
            self._call(self.api.login, user, self.cfg.admin_password)
        except UptimeKumaException as exc:
            raise ProvisionError(
                f"Kuma refused the login of '{user}': {str(exc).rstrip('.')}. If the password was changed in the UI "
                "(or 2FA was switched on), make the config match or switch 2FA off for this account."
            ) from exc
        LOG.info("Logged in to Kuma as '%s'", user)
        try:  # only informative: the version arrives with the first post-login info event
            LOG.info("Kuma server version %s", self._call(self.api.info).get("version", "unknown"))
        except Timeout:
            LOG.warning("Kuma did not report its version (continuing)")
        return True

    def ensure_settings(self) -> None:
        """Set Primary Base URL and Trust Proxy, keeping every other setting as it is.

        Kuma's ``setSettings`` replaces ALL settings, and the client resets every
        omitted one to its default, so the current values are read and sent back.
        """
        wanted = {"primaryBaseURL": f"https://{self.cfg.domain}", "trustProxy": True}
        current = self._call(self.api.get_settings)
        changes = {key: value for key, value in wanted.items() if current.get(key) != value}
        if not changes:
            LOG.info("Settings already correct")
            self.stats["unchanged"] += 1
            return
        if self._write(f"update the settings {sorted(changes)}", "updated"):
            accepted = set(inspect.signature(self.api.set_settings).parameters) - {"password"}
            merged = {key: value for key, value in current.items() if key in accepted}
            merged.update(changes)
            self._call(self.api.set_settings, **merged)

    def ensure_notification(self) -> None:
        """Create or update the Telegram channel (not default, not applied to old monitors).

        Raises:
            ProvisionError: If a notification with that name exists but is not Telegram.
        """
        name = self.cfg.notification_name
        desired = {"telegramBotToken": self.cfg.telegram_token, "telegramChatID": self.cfg.telegram_chat_id}
        existing = next(
            (n for n in self._call(self.api.get_notifications) if n.get("name") == name), None
        )
        if existing is None:
            if self._write(f"create the Telegram notification '{name}'", "created"):
                response = self._call(
                    self.api.add_notification, name=name, type=NotificationType.TELEGRAM,
                    isDefault=False, applyExisting=False, **desired,
                )
                self._notification_id = int(response["id"])
            return
        if existing.get("type") != NotificationType.TELEGRAM:
            raise ProvisionError(f"a notification named '{name}' exists but is not a Telegram one")
        self._notification_id = int(existing["id"])
        changes = {key: value for key, value in desired.items() if existing.get(key) != value}
        if not changes:
            LOG.info("Notification '%s' already correct", name)
            self.stats["unchanged"] += 1
        elif self._write(f"update the notification '{name}' ({sorted(changes)})", "updated"):
            self._call(self.api.edit_notification, self._notification_id, **changes)

    def _index(self) -> dict[tuple[int | None, str], dict]:
        """Index all monitors by (parent id, name); also refreshes ``self._monitors``."""
        self._monitors = self._call(self.api.get_monitors)
        return {(m.get("parent"), m["name"]): m for m in self._monitors}

    def ensure_groups(self) -> None:
        """Create the Group monitors (no alerts attached, see the module docstring).

        Raises:
            ProvisionError: If a top-level monitor with a Group's name is not a Group.
        """
        index = self._index()
        for group in self.cfg.groups:
            existing = index.get((None, group))
            if existing is None:
                if self._write(f"create the Group '{group}'", "created"):
                    response = self._call(self.api.add_monitor, type=MonitorType.GROUP, name=group)
                    self._group_ids[group] = int(response["monitorID"])
                else:
                    self._group_ids[group] = -1
            elif existing.get("type") != MonitorType.GROUP:
                raise ProvisionError(f"a monitor named '{group}' exists but is not a Group")
            else:
                self._group_ids[group] = int(existing["id"])
                LOG.info("Group '%s' exists (id %s)", group, existing["id"])
                self.stats["unchanged"] += 1

    def _payload(self, spec: TargetSpec) -> dict[str, Any]:
        """Everything Kuma needs to know about one Target."""
        return {
            "type": MonitorType.KEYWORD,
            "name": spec.name,
            "parent": self._group_ids[spec.group],
            "url": spec.url,
            "keyword": spec.marker,
            "method": "GET",
            "interval": spec.interval,
            "retryInterval": spec.retry_interval,
            "maxretries": spec.retries,
            "resendInterval": spec.resend_interval,
            "timeout": spec.timeout,
            "maxredirects": 10,
            "accepted_statuscodes": ["200-299"],
            "notificationIDList": [self._notification_id],
        }

    @staticmethod
    def _changes(existing: dict, desired: dict) -> dict[str, Any]:
        """Fields of ``desired`` that differ from the monitor Kuma already has."""
        changes = {field: desired[field] for field in COMPARED_FIELDS if existing.get(field) != desired[field]}
        if sorted(existing.get("notificationIDList") or []) != sorted(desired["notificationIDList"]):
            changes["notificationIDList"] = desired["notificationIDList"]
        return changes

    def ensure_targets(self) -> None:
        """Create or update every Target as a keyword monitor inside its Group."""
        index = self._index()
        for spec in self.cfg.targets:
            desired = self._payload(spec)
            existing = index.get((desired["parent"], spec.name))
            label = f"Target '{spec.name}' in '{spec.group}'"
            if existing is None:
                if self._write(f"create {label} -> {spec.url}", "created"):
                    self._call(self.api.add_monitor, **desired)
                continue
            changes = self._changes(existing, desired)
            if not changes:
                LOG.info("%s already correct", label)
                self.stats["unchanged"] += 1
            elif self._write(f"update {label} ({sorted(changes)})", "updated"):
                self._call(self.api.edit_monitor, existing["id"], **changes)

    def _unmanaged(self) -> list[dict]:
        """Monitors Kuma has that the config does not describe, children before their Groups."""
        wanted = {(self._group_ids[t.group], t.name) for t in self.cfg.targets}
        wanted |= {(None, group) for group in self.cfg.groups}
        extra = [m for m in self._monitors if (m.get("parent"), m["name"]) not in wanted]
        # The API client deletes a Group WITHOUT its children (they would only be unlinked),
        # so children go first.
        return sorted(extra, key=lambda m: m.get("parent") is None)

    def handle_unmanaged(self) -> None:
        """Report monitors the config does not describe, or delete them when pruning."""
        extra = self._unmanaged()
        if not extra:
            return
        if not self.prune:
            LOG.warning("Monitors in Kuma that are not in the config (left untouched, --prune would delete them): %s",
                        [m["name"] for m in extra])
            return
        names = {m["id"]: m["name"] for m in self._monitors}
        for monitor in extra:
            parent = names.get(monitor.get("parent"))
            label = f"monitor '{monitor['name']}'" + (f" of group '{parent}'" if parent else "")
            if self._write(f"delete {label}", "deleted"):
                self._call(self.api.delete_monitor, monitor["id"])

    def run(self) -> None:
        """Apply everything, in dependency order."""
        self.let_server_greet()
        if not self.login():
            return
        self.ensure_settings()
        self.ensure_notification()
        self.ensure_groups()
        self.ensure_targets()
        self.handle_unmanaged()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` when None.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Provision Uptime Kuma from the Monitoring VM Private config.")
    parser.add_argument("--url", required=True, help="Kuma base URL, e.g. http://10.43.12.34:3001")
    parser.add_argument("--config", required=True, help="Private config file (YAML)")
    parser.add_argument("--run-id", default="manual", help="RunId put into every log record")
    parser.add_argument("--log-file", help="also write DEBUG logs (full API bodies, secrets masked) here")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="console log level (default INFO)")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds to wait for each API answer (default 20)")
    parser.add_argument("--site", help="id of the site (domain + Groups) this Kuma serves; required when the config has several")
    parser.add_argument("--check", action="store_true", help="dry run: log what would change, write nothing")
    parser.add_argument("--prune", action="store_true",
                        help="also DELETE monitors the config does not describe (use with --check first)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` when None.

    Returns:
        The process exit code (see the module docstring).
    """
    args = parse_args(argv)
    formatter = setup_logging(args.run_id, args.log_file, args.log_level)
    LOG.info("Kuma provisioning started (%s%s)", "CHECK ONLY, nothing will be written" if args.check else "applying",
             ", PRUNE: unmanaged monitors are deleted" if args.prune else "")

    try:
        config = load_config(args.config, args.site)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return EXIT_CONFIG
    formatter.add_secrets(config.secrets)
    LOG.info("Site '%s': %d Group(s) %s, %d Target(s), UI https://%s", config.site or "-", len(config.groups),
             config.groups, len(config.targets), config.domain)

    provisioner: Provisioner | None = None
    try:
        with connect(args.url, args.timeout) as api:
            provisioner = Provisioner(api, config, args.check, args.prune)
            provisioner.run()
    except (ProvisionError, UptimeKumaException) as exc:
        LOG.error("%s: %s", type(exc).__name__, exc)  # a Timeout has an empty message
        return EXIT_FAILURE
    except Exception:
        LOG.exception("Unexpected failure")
        return EXIT_FAILURE

    stats = provisioner.stats
    changed = stats["created"] + stats["updated"] + stats["deleted"]
    LOG.info("RESULT changed=%d created=%d updated=%d deleted=%d unchanged=%d check=%s",
             changed, stats["created"], stats["updated"], stats["deleted"], stats["unchanged"], args.check)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

# code-documentation-2026-10-20T18:43:26+03:00
