"""生成本地/内网自签证书 → deploy/certs/"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CERT_DIR = ROOT / "deploy" / "certs"
CRT = CERT_DIR / "fullchain.pem"
KEY = CERT_DIR / "privkey.pem"


def main() -> int:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if CRT.exists() and KEY.exists():
        print(f"certs_exist {CRT}")
        return 0

    # 优先 openssl；没有则用 cryptography 若已装；再没有用 PowerShell 不适合服务端
    openssl = None
    for cand in ("openssl", r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe"):
        try:
            subprocess.run(
                [cand, "version"],
                check=True,
                capture_output=True,
                text=True,
            )
            openssl = cand
            break
        except Exception:
            continue

    if openssl:
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-days",
                "825",
                "-nodes",
                "-keyout",
                str(KEY),
                "-out",
                str(CRT),
                "-subj",
                "/CN=localhost/O=private-kb/C=CN",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
        )
        print(f"openssl_ok {CRT}")
        return 0

    # fallback: pure python via cryptography if present
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print(
            "NO_OPENSSL_AND_NO_CRYPTOGRAPHY\n"
            "请安装 OpenSSL，或: uv add cryptography\n"
            "然后重跑: uv run python scripts/gen_self_signed_cert.py"
        )
        return 2

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "private-kb"),
        ]
    )
    alt = x509.SubjectAlternativeName(
        [
            x509.DNSName("localhost"),
            x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1")),
        ]
    )
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(alt, critical=False)
        .sign(key, hashes.SHA256())
    )
    KEY.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CRT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"cryptography_ok {CRT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
