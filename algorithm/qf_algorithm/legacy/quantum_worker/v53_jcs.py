"""RFC 8785 JSON Canonicalization Scheme for V5.3 cross-language hashes.

The TypeScript verifier uses ECMAScript ``JSON.stringify`` for finite IEEE-754
numbers and unsigned UTF-16 code-unit ordering for object names.  Python's
``json.dumps`` differs for integral floats, exponent spelling, fixed/scientific
thresholds, and astral-name ordering, so security-significant V5.3 digests must
not use it directly.
"""

from __future__ import annotations

import json
import math
from typing import Any, NoReturn


class V53JcsError(ValueError):
    """A value cannot be represented in the strict RFC 8785/I-JSON domain."""


def _reject(reason: str) -> NoReturn:
    raise V53JcsError(reason)


def _assert_well_formed_unicode(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _reject("strings and property names must not contain lone surrogates")


def _utf16_sort_key(value: str) -> tuple[int, ...]:
    """Return unsigned UTF-16 code units, the RFC 8785 property-name order."""

    _assert_well_formed_unicode(value)
    encoded = value.encode("utf-16-be")
    return tuple(
        int.from_bytes(encoded[offset : offset + 2], "big")
        for offset in range(0, len(encoded), 2)
    )


def _serialize_finite_double(value: float) -> str:
    """Render one binary64 using ECMAScript Number::toString/JCS thresholds.

    CPython's ``repr(float)`` supplies the shortest round-trippable significand
    for the same binary64 value.  This function only converts that significand
    to ECMAScript's fixed/scientific layout and exponent spelling.
    """

    if not math.isfinite(value):
        _reject("numbers must be finite IEEE-754 binary64 values")
    if value == 0:
        return "0"

    sign = "-" if value < 0 else ""
    rendered = repr(abs(value)).lower()
    if "e" in rendered:
        coefficient, exponent_text = rendered.split("e", 1)
        exponent = int(exponent_text)
    else:
        coefficient = rendered
        exponent = 0

    if "." in coefficient:
        integer_part, fractional_part = coefficient.split(".", 1)
    else:
        integer_part, fractional_part = coefficient, ""

    raw_digits = integer_part + fractional_part
    leading_zeroes = len(raw_digits) - len(raw_digits.lstrip("0"))
    digits = raw_digits[leading_zeroes:].rstrip("0")
    if not digits:
        return "0"

    # value = digits * 10 ** (n - k), where k is the digit count.
    n = len(integer_part) + exponent - leading_zeroes
    k = len(digits)

    if k <= n <= 21:
        body = digits + ("0" * (n - k))
    elif 0 < n <= 21:
        body = f"{digits[:n]}.{digits[n:]}"
    elif -6 < n <= 0:
        body = f"0.{('0' * -n)}{digits}"
    else:
        significand = digits if k == 1 else f"{digits[0]}.{digits[1:]}"
        scientific_exponent = n - 1
        exponent_sign = "+" if scientific_exponent >= 0 else ""
        body = f"{significand}e{exponent_sign}{scientific_exponent}"
    return sign + body


def _serialize_number(value: int | float) -> str:
    if isinstance(value, bool):
        _reject("booleans are not numbers in the JCS serializer")
    if isinstance(value, int):
        try:
            double = float(value)
        except OverflowError:
            _reject("integers must be representable as finite IEEE-754 binary64")
        if not math.isfinite(double):
            _reject("integers must be representable as finite IEEE-754 binary64")
        return _serialize_finite_double(double)
    if type(value) is not float:
        _reject("numbers must be plain Python int or float values")
    return _serialize_finite_double(value)


def _serialize(value: Any, ancestors: set[int]) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) in (int, float):
        return _serialize_number(value)
    if type(value) is str:
        _assert_well_formed_unicode(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    if type(value) not in (list, dict):
        _reject("values must be plain acyclic JSON lists and dictionaries")

    identity = id(value)
    if identity in ancestors:
        _reject("values must be acyclic")
    ancestors.add(identity)
    try:
        if type(value) is list:
            return "[" + ",".join(_serialize(item, ancestors) for item in value) + "]"

        entries: list[str] = []
        for key in value:
            if type(key) is not str:
                _reject("object property names must be strings")
            _assert_well_formed_unicode(key)
        for key in sorted(value, key=_utf16_sort_key):
            encoded_key = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            entries.append(f"{encoded_key}:{_serialize(value[key], ancestors)}")
        return "{" + ",".join(entries) + "}"
    finally:
        ancestors.remove(identity)


def canonical_v53_json(value: Any) -> str:
    """Return the exact RFC 8785 canonical text for a strict I-JSON value."""

    return _serialize(value, set())


def canonical_v53_json_bytes(value: Any) -> bytes:
    """Return canonical UTF-8 bytes used by all Python-origin V5.3 hashes."""

    return canonical_v53_json(value).encode("utf-8")
