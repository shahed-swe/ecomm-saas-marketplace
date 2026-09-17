from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_DUMMY = _ph.hash("dummy-password-for-timing")
MIN_LENGTH = 10


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hashed: str | None, pw: str) -> bool:
    """Constant-ish time: always runs one argon2 verify, even for unknown users."""
    try:
        return _ph.verify(hashed or _DUMMY, pw) and hashed is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
