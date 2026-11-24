#!/home/dietpi/tapo-automation/tapo-env/bin/python3
"""
phone_probe.py - is anyone home? Probes every phone with several methods at once.

    phone_probe.py [--json] [--verbose] [--timeout SECS] [IP ...]

Methods (all phones and methods run in parallel, the first positive answer wins):

  icmp  ping. Fast and simple, but a sleeping iPhone ignores it.
  tcp   TCP connect to a port. "Open" (iPhones keep 62078 open) and
        "connection refused" both prove the phone is on the network; only
        silence does not.
  arp   the kernel neighbour table. A phone that answers ARP (they all do while
        on wifi, even asleep) shows as REACHABLE for ~30 s after any packet we
        sent it, so this is checked last, after the ping/TCP attempts.

Phones and options come from config.json ("phones" and "presence"), unless IPs
are given on the command line.

Exit codes: 0 someone is home, 1 nobody found, 2 configuration or usage error.
Prints one line describing the outcome (it ends up in tapo.log on state changes).
"""
import argparse
import errno
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

DEFAULT_BASE_DIR = "/home/dietpi/tapo-automation"

PRESENCE_DEFAULTS = {
    "methods": ["icmp", "tcp", "arp"],
    "tcp_ports": [62078],       # iOS lockdownd; any port works, "refused" also counts
    "timeout_seconds": 1.5,     # how long to wait for answers, in total
    "ping": "ping",
    "ip": "ip",
}
KNOWN_METHODS = ("icmp", "tcp", "arp")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Phone:
    name: str
    ip: str


@dataclass
class PresenceConfig:
    methods: List[str]
    tcp_ports: List[int]
    timeout_seconds: float
    ping: str
    ip: str


@dataclass
class Result:
    found: Dict[str, str]              # phone name -> method that answered
    elapsed: float
    methods: List[str]
    details: Dict[str, Optional[bool]]  # "phone/method" -> True, False or None (unfinished)

    @property
    def home(self):
        return bool(self.found)


# --------------------------------------------------------------------------- config

def load_config(base_dir):
    path = os.path.join(base_dir, "config.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found")
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be an object")

    phones_in = data.get("phones")
    if not isinstance(phones_in, dict) or not phones_in:
        raise ConfigError(f"{path}: 'phones' must be a non-empty object of name -> {{\"ip\": ...}}")
    phones = []
    for name, entry in phones_in.items():
        ip = entry.get("ip") if isinstance(entry, dict) else entry
        if not isinstance(ip, str) or not ip:
            raise ConfigError(f"{path}: phone {name!r} needs an 'ip'")
        phones.append(Phone(name=name, ip=ip))

    presence = parse_presence(data.get("presence", {}), path)
    return phones, presence


def parse_presence(raw, path="config.json"):
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'presence' must be an object")
    unknown = set(raw) - set(PRESENCE_DEFAULTS)
    if unknown:
        raise ConfigError(f"{path}: unknown presence option(s): {', '.join(sorted(unknown))}")
    opts = dict(PRESENCE_DEFAULTS, **raw)
    methods = opts["methods"]
    if not isinstance(methods, list) or not methods or any(m not in KNOWN_METHODS for m in methods):
        raise ConfigError(f"{path}: presence.methods must be a non-empty list from {list(KNOWN_METHODS)}")
    ports = opts["tcp_ports"]
    if not isinstance(ports, list) or any(not isinstance(p, int) or not 0 < p < 65536 for p in ports):
        raise ConfigError(f"{path}: presence.tcp_ports must be a list of port numbers")
    timeout = opts["timeout_seconds"]
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"{path}: presence.timeout_seconds must be a positive number")
    return PresenceConfig(methods=list(methods), tcp_ports=list(ports), timeout_seconds=float(timeout),
                          ping=str(opts["ping"]), ip=str(opts["ip"]))


# --------------------------------------------------------------------------- the methods

class Prober:
    """The real network probes. Each returns True when the phone is there."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._procs = []
        self._lock = threading.Lock()

    def icmp(self, phone):
        wait = str(max(1, math.ceil(self.cfg.timeout_seconds)))
        try:
            proc = subprocess.Popen([self.cfg.ping, "-c", "1", "-W", wait, phone.ip],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            return False
        with self._lock:
            self._procs.append(proc)
        try:
            return proc.wait(timeout=self.cfg.timeout_seconds + 1) == 0
        except subprocess.TimeoutExpired:
            _kill_and_reap(proc)
            return False

    def tcp(self, phone, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.cfg.timeout_seconds)
        try:
            rc = s.connect_ex((phone.ip, port))
        except OSError:
            return False
        finally:
            s.close()
        # open port, or an RST ("refused"): either way something is answering
        return rc in (0, errno.ECONNREFUSED)

    def arp(self, phone):
        try:
            out = subprocess.run([self.cfg.ip, "-4", "neigh", "show", "to", phone.ip],
                                 capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return any("REACHABLE" in line.split() for line in out.stdout.splitlines())

    def cleanup(self):
        """Kill pings that are still running after an early exit."""
        with self._lock:
            procs, self._procs = self._procs, []
        for proc in procs:
            if proc.poll() is None:
                _kill_and_reap(proc)


def _kill_and_reap(proc):
    try:
        proc.kill()
        proc.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


# --------------------------------------------------------------------------- orchestration

def probe(phones, cfg, prober, clock=time.monotonic):
    """Run every enabled method for every phone in parallel; stop at the first hit."""
    started = clock()
    found = {}
    details = {}
    lock = threading.Lock()
    hit = threading.Event()

    def task(phone, label, fn):
        details_key = f"{phone.name}/{label}"
        ok = False
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        with lock:
            details[details_key] = ok
            if ok:
                found.setdefault(phone.name, label)
                hit.set()

    threads = []
    for phone in phones:
        if "icmp" in cfg.methods:
            details[f"{phone.name}/icmp"] = None
            threads.append(threading.Thread(target=task, args=(phone, "icmp", lambda p=phone: prober.icmp(p)),
                                            daemon=True))
        if "tcp" in cfg.methods:
            for port in cfg.tcp_ports:
                details[f"{phone.name}/tcp:{port}"] = None
                threads.append(threading.Thread(
                    target=task, args=(phone, f"tcp:{port}", lambda p=phone, pt=port: prober.tcp(p, pt)),
                    daemon=True))
    for t in threads:
        t.start()

    deadline = started + cfg.timeout_seconds + 0.5
    while not hit.is_set() and any(t.is_alive() for t in threads) and clock() < deadline:
        hit.wait(0.02)

    if not found and "arp" in cfg.methods:
        # after the pings/SYNs above the kernel has just tried to resolve every
        # phone, so a phone that answered ARP is REACHABLE right now
        for phone in phones:
            ok = False
            try:
                ok = bool(prober.arp(phone))
            except Exception:
                ok = False
            with lock:
                details[f"{phone.name}/arp"] = ok
                if ok:
                    found.setdefault(phone.name, "arp")

    prober.cleanup()
    with lock:
        return Result(found=dict(found), elapsed=clock() - started, methods=list(cfg.methods),
                      details=dict(details))


def method_labels(cfg):
    labels = []
    for m in cfg.methods:
        if m == "tcp":
            labels += [f"tcp:{p}" for p in cfg.tcp_ports]
        else:
            labels.append(m)
    return labels


def summary(result, phones, cfg):
    if result.home:
        who = ", ".join(f"{name} via {method}" for name, method in result.found.items())
        return f"home: {who} ({result.elapsed:.1f}s)"
    names = ", ".join(p.name for p in phones)
    return f"away: no answer from {names} via {','.join(method_labels(cfg))} ({result.elapsed:.1f}s)"


# --------------------------------------------------------------------------- cli

def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("ips", nargs="*", metavar="IP", help="probe these IPs instead of the configured phones")
    p.add_argument("--base-dir", default=os.environ.get("TAPO_BASE_DIR", DEFAULT_BASE_DIR))
    p.add_argument("--timeout", type=float, metavar="SECS", help="override presence.timeout_seconds")
    p.add_argument("--json", action="store_true", help="machine readable output")
    p.add_argument("--verbose", action="store_true", help="show every method's answer on stderr")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.ips:
            phones = [Phone(name=ip, ip=ip) for ip in args.ips]
            try:
                _, cfg = load_config(args.base_dir)
            except ConfigError:
                cfg = parse_presence({})
        else:
            phones, cfg = load_config(args.base_dir)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.timeout:
        cfg.timeout_seconds = args.timeout

    result = probe(phones, cfg, Prober(cfg))

    if args.verbose:
        for key in sorted(result.details):
            state = {True: "yes", False: "no", None: "no answer in time"}[result.details[key]]
            print(f"  {key}: {state}", file=sys.stderr)
    if args.json:
        print(json.dumps({"home": result.home, "found": result.found, "elapsed": round(result.elapsed, 2),
                          "methods": method_labels(cfg), "details": result.details}))
    else:
        print(summary(result, phones, cfg))
    return 0 if result.home else 1


if __name__ == "__main__":
    sys.exit(main())
