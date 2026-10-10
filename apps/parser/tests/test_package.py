import tomllib
from importlib.metadata import version
from pathlib import Path

import docparser

PARSER = Path(__file__).resolve().parents[1]


def test_version_constant_matches_the_project() -> None:
    project = tomllib.loads((PARSER / "pyproject.toml").read_text())["project"]
    assert docparser.__version__ == project["version"] == version("docparser")


def test_image_requirements_match_the_lock() -> None:
    """requirements.txt (the image's hash-locked install list) is exported from uv.lock."""
    text = (PARSER / "requirements.txt").read_text()
    lock = tomllib.loads((PARSER / "uv.lock").read_text())
    runtime = {"pdfminer-six", "cryptography", "cffi", "pycparser", "charset-normalizer"}
    for package in lock["package"]:
        if package["name"] in runtime:
            assert f"{package['name']}=={package['version']}" in text
    assert "--hash=sha256:" in text
