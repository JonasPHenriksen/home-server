"""
home-certs: the certificates for the home sites, and the CA the devices trust,
whose key must be gone once they are made.
"""
import os
import ssl
import stat
import subprocess

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def openssl(*args):
    return subprocess.run(["openssl", *map(str, args)], capture_output=True, text=True).stdout


def make(tmp_path):
    env = dict(os.environ, HOME_TLS_DIR=str(tmp_path / "tls"))
    return subprocess.run([str(ROOT / "home-certs")], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60)


def test_certificates_an_iphone_accepts_and_no_ca_key(tmp_path):
    assert make(tmp_path).returncode == 0
    tls = tmp_path / "tls"
    assert sorted(p.name for p in tls.iterdir()) == ["ca.crt", "desktop.crt", "desktop.key",
                                                     "laptop.crt", "laptop.key"], "the CA's key is gone"
    assert stat.S_IMODE(tls.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in tls.iterdir())
    ca = openssl("x509", "-noout", "-text", "-in", tls / "ca.crt")
    assert "CA:TRUE" in ca, "an iPhone offers full trust only for a CA"
    names = {"laptop": ["IP Address:192.168.50.10", "IP Address:100.64.0.10", "DNS:dietpi-laptop",
                        "DNS:dietpi-laptop.tailnet-example.ts.net", "DNS:pi.hole"],
             "desktop": ["IP Address:192.168.50.20", "IP Address:100.64.0.20", "DNS:dietpi",
                         "DNS:dietpi.tailnet-example.ts.net"]}
    for machine, want in names.items():
        crt = tls / f"{machine}.crt"
        assert f"{crt}: OK" in openssl("verify", "-CAfile", tls / "ca.crt", crt)
        text = openssl("x509", "-noout", "-text", "-in", crt)
        for w in want + ["CA:FALSE", "TLS Web Server Authentication", "ecdsa-with-SHA256", "P-256"]:
            assert w in text, (machine, w)
        dates = dict(line.split("=", 1) for line in
                     openssl("x509", "-noout", "-startdate", "-enddate", "-in", crt).splitlines())
        days = (ssl.cert_time_to_seconds(dates["notAfter"]) - ssl.cert_time_to_seconds(dates["notBefore"])) / 86400
        assert days <= 825, "Apple's limit for a TLS certificate"
        key_pub = openssl("pkey", "-in", tls / f"{machine}.key", "-pubout")
        assert key_pub and key_pub == openssl("x509", "-noout", "-pubkey", "-in", crt), "key and certificate belong together"


def test_new_certificates_are_not_made_by_accident(tmp_path):
    assert make(tmp_path).returncode == 0
    before = (tmp_path / "tls" / "ca.crt").read_text()
    r = make(tmp_path)
    assert r.returncode == 1 and "trusted again" in r.stdout
    assert (tmp_path / "tls" / "ca.crt").read_text() == before
