import hashlib
from enum import StrEnum
from typing import Protocol

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key


class AppKeyFile(Protocol):
    def read(self) -> bytes | None: ...

    def write_new(self, public_key_der: bytes) -> None: ...


class PairOutcome(StrEnum):
    PAIRED = "paired"
    SAME_KEY = "same_key"
    ALREADY_PAIRED = "already_paired"
    NOT_A_P256_KEY = "not_a_p256_key"


class AppPairing:
    def __init__(self, key_file: AppKeyFile) -> None:
        self._key_file = key_file
        der = key_file.read()
        self._public_key = None if der is None else _p256_public_key(der)

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey | None:
        return self._public_key

    def pair(self, public_key_der: bytes) -> PairOutcome:
        try:
            public_key = _p256_public_key(public_key_der)
        except ValueError:
            return PairOutcome.NOT_A_P256_KEY
        if self._public_key is not None:
            return (
                PairOutcome.SAME_KEY
                if public_key == self._public_key
                else PairOutcome.ALREADY_PAIRED
            )
        try:
            self._key_file.write_new(public_key_der)
        except FileExistsError:
            return PairOutcome.ALREADY_PAIRED
        self._public_key = public_key
        return PairOutcome.PAIRED


def key_fingerprint(public_key_der: bytes) -> str:
    return hashlib.sha256(public_key_der).hexdigest()[:16]


def _p256_public_key(der: bytes) -> ec.EllipticCurvePublicKey:
    try:
        public_key = load_der_public_key(der)
    except (ValueError, UnsupportedAlgorithm) as error:
        raise ValueError("不是 DER 编码的公钥") from error
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise ValueError("不是 P-256 公钥")
    return public_key
