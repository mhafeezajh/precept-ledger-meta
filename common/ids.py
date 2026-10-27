"""ULIDs for records, escalations and drafts.

Written here (about 20 lines) instead of adding a dependency, so the timestamp comes from
an injected clock and the randomness from ``secrets`` (skill §3, §4).
"""

import re
import secrets

from common.clock import Clock

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# 26 Crockford chars; the first is at most 7 because a ULID is 128 bits.
ULID_PATTERN = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def new_ulid(clock: Clock) -> str:
    """48-bit millisecond timestamp followed by 80 random bits, Crockford base32."""
    millis = int(clock.now().timestamp() * 1000)
    if not 0 <= millis < 2**48:
        raise ValueError("clock time outside the ULID range")
    value = (millis << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def is_ulid(text: str) -> bool:
    return ULID_PATTERN.fullmatch(text) is not None
