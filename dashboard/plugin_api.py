"""The two read-only answers the Desktop part may ask for: the status and the
Files list. Nothing here writes, restores, saves or deletes. A Desktop plugin
in Hermes runs with the whole app's authority, so these routes are the whole
of what that authority can reach in this plugin.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()
_ROOT = Path(__file__).resolve().parent.parent


def _load():
    """Import the plugin package by path, whatever name Hermes gave it."""
    name = "signet_drive_plugin"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ROOT / "__init__.py",
                                                  submodule_search_locations=[str(_ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _status():
    mod = _load()
    return mod.sd_status.Status(mod._data_dir())


@router.get("/status")
async def status():
    return _status().summary()


@router.get("/files")
async def files():
    mod = _load()
    data_dir = mod._data_dir()
    # The config the agent half read is kept beside the status for the view.
    st = _status().read()
    cfg = st.get("config") or {}
    client = mod.SignetClient(binary=cfg.get("binary", "signet"), server_url=cfg.get("server_url") or None,
                              handle=cfg.get("handle") or None)
    try:
        folder = mod.sd_tools.FilesFolder(client, cfg.get("files_folder", "Files"))
        rows = [{"name": n, "size_bytes": r.get("size_bytes"), "modified_at": r.get("modified_at")}
                for n, r in sorted(folder.names().items())]
        copies = mod.sd_mirror.Mirror(client, mod._hermes_home(), data_dir,
                                      root_name=cfg.get("root_name", "Memory Mirror")).list_points()
        return {"files": rows, "copies": copies[-40:], "error": None}
    except Exception as e:  # the view shows the reason, the app stays up
        return {"files": [], "copies": [], "error": str(e)}
