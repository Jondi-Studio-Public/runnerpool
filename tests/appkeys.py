"""Throwaway RSA keys for the GitHub App tests: generated with the system openssl, never a real key."""

import base64
import shutil
import subprocess
from pathlib import Path

OPENSSL = shutil.which("openssl")


def run(*args, stdin=None):
    return subprocess.run([OPENSSL, *args], input=stdin, capture_output=True, check=True)


def make_keys(folder: Path):
    """Write key-pkcs1.pem (BEGIN RSA PRIVATE KEY, what GitHub issues), key-pkcs8.pem and key-pub.pem."""
    folder.mkdir(parents=True, exist_ok=True)
    pkcs8 = folder / "key-pkcs8.pem"
    pkcs8.write_bytes(run("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048").stdout)
    pkcs1 = folder / "key-pkcs1.pem"
    pkcs1.write_bytes(run("rsa", "-in", str(pkcs8), "-traditional").stdout)
    assert pkcs1.read_text().startswith("-----" + "BEGIN RSA PRIVATE KEY" + "-----")
    pub = folder / "key-pub.pem"
    pub.write_bytes(run("rsa", "-in", str(pkcs8), "-pubout").stdout)
    return pkcs1, pkcs8, pub


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_jwt(jwt: str, pub: Path, tmp: Path) -> bool:
    """True when the JWT's RS256 signature checks out against the public key."""
    head, body, sig = jwt.split(".")
    (tmp / "jwt.sig").write_bytes(b64url_decode(sig))
    (tmp / "jwt.msg").write_bytes(f"{head}.{body}".encode())
    r = subprocess.run(
        [OPENSSL, "dgst", "-sha256", "-verify", str(pub), "-signature", str(tmp / "jwt.sig"), str(tmp / "jwt.msg")],
        capture_output=True,
    )
    return r.returncode == 0
