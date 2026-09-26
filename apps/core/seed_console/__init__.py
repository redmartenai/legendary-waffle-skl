"""Design data for the principal's web console, one module per area.

Each module exposes ``seed(cmd, school)``. ``cmd`` is the running ``seed_design`` command, so modules can
use its helpers (``cmd._user``, ``cmd._phone``, ``cmd._school_day``, ``cmd._invoice``) and what it built
(``cmd.groups``, ``cmd.students``, ``cmd.parent``, ``cmd.today``). They run in this order, after
everything else, inside the seed's transaction and school context.
"""

from importlib import import_module

MODULES = ("people", "academics", "operations", "engage", "settings_attendance", "dashboard")


def run(cmd, school, only: str | None = None):
    for name in MODULES:
        if only and name != only:
            continue
        module = import_module(f"{__name__}.{name}")
        module.seed(cmd, school)
