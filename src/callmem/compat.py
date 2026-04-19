"""Python version compatibility shims."""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from datetime import UTC
else:
    from datetime import timezone

    UTC = timezone.utc

__all__ = ["UTC"]
