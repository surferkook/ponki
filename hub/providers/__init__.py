"""Model providers (spec 03 grows the Meta Model API client here).

Step 1 only needs the model's name for /api/info; souls arrive in step 2.
"""
from __future__ import annotations

import os


class StubProvider:
    name = "meta"

    def __init__(self) -> None:
        self.model = os.environ.get("HOUSE_MODEL", "muse-spark-1.3")
