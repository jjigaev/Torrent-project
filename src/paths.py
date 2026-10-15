"""Portable validation for untrusted torrent path components."""

from pathlib import Path


def validate_component(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("Invalid file name")
    if any(ord(char) < 32 or char in '/\\:<>"|?*' for char in value):
        raise ValueError("Invalid file name")
    if value.endswith((".", " ")):
        raise ValueError("Invalid file name")
    stem = value.split(".", 1)[0].rstrip(" ").upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³")
    if stem in reserved:
        raise ValueError("Reserved file name")
    return value


def safe_join(directory, *components):
    base = Path(directory).resolve()
    candidate = base.joinpath(*(validate_component(part) for part in components)).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise ValueError("Output path escapes its directory")
    return candidate
