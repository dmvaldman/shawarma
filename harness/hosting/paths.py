"""Relative native-service destinations cannot redirect outside their service."""

from urllib.parse import unquote


def service_path(value: str) -> str:
    decoded = unquote(value)
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(c) < 32 for c in decoded)
    ):
        raise ValueError("Invalid service path")
    return value
