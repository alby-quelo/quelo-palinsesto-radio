# -*- coding: utf-8 -*-
"""Risoluzione path database Quelo-palinsesto-radio."""

from __future__ import annotations

import os
from pathlib import Path

QUELO_HOME_MNT = Path("/media/quelo-home")
DB_NAME = ".palinsesto.db"


def resolve_db_path() -> Path:
    """Live con QUELO-HOME montato → /media/quelo-home/.palinsesto.db
    altrimenti → $HOME/.palinsesto.db
    """
    try:
        if QUELO_HOME_MNT.is_dir() and os.path.ismount(QUELO_HOME_MNT):
            return QUELO_HOME_MNT / DB_NAME
    except OSError:
        pass
    home = Path(os.environ.get("HOME") or Path.home())
    return home / DB_NAME
