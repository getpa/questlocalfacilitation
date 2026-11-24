"""Local CA and server certificate for the HTTPS viewer (docs/GUI_DESIGN.md §4.6).

The CA is created once per Mac and installed on each viewing device. The
server certificate is re-issued whenever the Mac's addresses change; the CA
stays, so devices do not need to be touched again. Validity is kept under
Apple's limit for server certificates (825 days; we use 397).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
from pathlib import Path
import plistlib
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
SERVER_DAYS = 397
RENEW_BEFORE_DAYS = 30


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


class LocalCA:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.ca_key_path = self.dir / "ca.key"
        self.ca_cert_path = self.dir / "ca.crt"
        self.server_key_path = self.dir / "server.key"
        self.server_cert_path = self.dir / "server.crt"

    # ------------------------------------------------------------ CA

    def ensure_ca(self) -> x509.Certificate:
        if self.ca_cert_path.exists() and self.ca_key_path.exists():
            return self.ca_cert()
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "Quest Hub Local CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Quest Hub"),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5))
            .not_valid_after(_now() + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        _write_private(self.ca_key_path, key.private_bytes(serialization.Encoding.PEM,
                                                           serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
        self.ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cert

    def ca_cert(self) -> x509.Certificate:
        return x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())

    def ca_key(self):
        return serialization.load_pem_private_key(self.ca_key_path.read_bytes(), password=None)

    def ca_pem(self) -> bytes:
        return self.ensure_ca().public_bytes(serialization.Encoding.PEM)

    def ca_der(self) -> bytes:
        return self.ensure_ca().public_bytes(serialization.Encoding.DER)

    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.ca_der()).hexdigest().upper()
        return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))

    # ------------------------------------------------------------ server

    def ensure_server_cert(self, hostnames: list[str], ips: list[str]) -> tuple[Path, Path]:
        """(cert, key) paths covering exactly these names; re-issued when they change."""
        self.ensure_ca()
        wanted = {f"DNS:{h}" for h in hostnames} | {f"IP:{ip}" for ip in ips}
        if self.server_cert_path.exists() and self.server_key_path.exists():
            cert = x509.load_pem_x509_certificate(self.server_cert_path.read_bytes())
            if self._san(cert) == wanted and cert.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_BEFORE_DAYS) \
                    and cert.issuer == self.ca_cert().subject:
                return self.server_cert_path, self.server_key_path
        key = ec.generate_private_key(ec.SECP256R1())
        alt = [x509.DNSName(h) for h in hostnames] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
        ca_cert, ca_key = self.ca_cert(), self.ca_key()
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0] if hostnames else ips[0])]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5))
            .not_valid_after(_now() + dt.timedelta(days=SERVER_DAYS))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=False,
                                         crl_sign=False, content_commitment=False, data_encipherment=False,
                                         key_agreement=True, encipher_only=False, decipher_only=False),
                           critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write_private(self.server_key_path, key.private_bytes(serialization.Encoding.PEM,
                                                               serialization.PrivateFormat.PKCS8,
                                                               serialization.NoEncryption()))
        self.server_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM)
                                          + ca_cert.public_bytes(serialization.Encoding.PEM))
        return self.server_cert_path, self.server_key_path

    @staticmethod
    def _san(cert: x509.Certificate) -> set[str]:
        try:
            ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            return set()
        return {f"DNS:{n}" for n in ext.get_values_for_type(x509.DNSName)} | \
               {f"IP:{n}" for n in ext.get_values_for_type(x509.IPAddress)}

    # ------------------------------------------------------------ iOS / iPadOS

    def mobileconfig(self) -> bytes:
        """Configuration profile that installs the CA on iPad / iPhone."""
        der = self.ca_der()
        payload_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, hashlib.sha256(der).hexdigest()))
        profile = {
            "PayloadContent": [{
                "PayloadCertificateFileName": "QuestHubLocalCA.cer",
                "PayloadContent": der,
                "PayloadDescription": "Quest Hub ビューワー用のローカル認証局",
                "PayloadDisplayName": "Quest Hub Local CA",
                "PayloadIdentifier": f"local.questhub.ca.{payload_uuid}",
                "PayloadType": "com.apple.security.root",
                "PayloadUUID": payload_uuid,
                "PayloadVersion": 1,
            }],
            "PayloadDisplayName": "Quest Hub ビューワー証明書",
            "PayloadDescription": "Quest Hub のビューワー（HTTPS）を開くための証明書です。",
            "PayloadIdentifier": "local.questhub.profile",
            "PayloadRemovalDisallowed": False,
            "PayloadType": "Configuration",
            "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_OID, "questhub-profile-" + payload_uuid)),
            "PayloadVersion": 1,
        }
        return plistlib.dumps(profile)
