"""Packaged static assets for the zero-build v1 Web console."""

from importlib.resources import files


def static_root():
    return files(__package__).joinpath("static")


__all__ = ["static_root"]
