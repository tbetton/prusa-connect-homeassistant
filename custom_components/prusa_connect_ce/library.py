"""The prusa-connect client library, as the integration imports it.

A release in the bundled mode carries a copy of the library in ``_vendor``
(generated from ``lib/`` by ``scripts/vendor_lib.py``) and uses it. A release
in the PyPI mode leaves the copy out and lists the package in manifest.json,
so the import falls back to the installed package. Everything in the
integration imports the library from here.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._vendor.prusa_connect import *
else:
    try:
        from ._vendor.prusa_connect import *
    except ModuleNotFoundError as err:
        if not (err.name or "").startswith(f"{__package__}._vendor"):
            raise
        from prusa_connect import *
