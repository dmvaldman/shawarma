"""Render analytics with the same typography tokens as the static portal."""

from importlib.resources import files
from pathlib import Path


def dashboard_page() -> str:
    package = files("harness.api")
    tokens = package.joinpath("typography.css")
    # Wheels include the canonical web asset; source checkouts read it directly.
    typography = (
        tokens.read_text()
        if tokens.is_file()
        else (Path(__file__).parents[2] / "web/typography.css").read_text()
    )
    return (
        package.joinpath("dashboard.html")
        .read_text()
        .replace("<style>", "<style>\n" + typography, 1)
    )
