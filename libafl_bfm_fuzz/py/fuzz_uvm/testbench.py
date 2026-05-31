from __future__ import annotations

import pyuvm

from fuzz_uvm.replay import LibAflUvmReplayTest


@pyuvm.test()
class LibAflUvmReplaySmokeTest(LibAflUvmReplayTest):
    """Generic pyUVM replay entry point for LibAFL-generated stimulus."""
