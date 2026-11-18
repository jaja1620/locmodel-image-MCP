"""Static list of standard-library modules the payload scripts import lazily.

The payload (`scripts/locmodel_image.py` and friends) is *not* part of the
installer's import graph: it is loaded from ``_MEIPASS/payload/scripts`` at
runtime through ``importlib``. PyInstaller therefore never walks it, and a
frozen build that misses e.g. ``urllib.error`` fails at ``--serve`` time with
``No module named 'urllib.error'`` while the pure-source install keeps working.

Importing this module once (the entry point does) puts every such module into
the dependency graph. Keep it in sync with the imports at the top of
``scripts/*.py``.
"""

from __future__ import annotations

# Mirrors scripts/locmodel_image.py, locmodel_edit.py, locmodel_mcp.py,
# locmodel_workflow.py — sorted, no wildcard imports.
import argparse  # noqa: F401
import base64  # noqa: F401
import copy  # noqa: F401
import datetime  # noqa: F401
import json  # noqa: F401
import logging  # noqa: F401
import os  # noqa: F401
import random  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import socket  # noqa: F401
import struct  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import traceback  # noqa: F401
import urllib.error  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401
import zlib  # noqa: F401

# Pulled in by urllib.request at runtime; listed explicitly so a future
# PyInstaller version cannot prune them.
import email  # noqa: F401
import email.parser  # noqa: F401
import http.client  # noqa: F401
import http.cookiejar  # noqa: F401
import ssl  # noqa: F401
import tempfile  # noqa: F401

__all__: list[str] = []
