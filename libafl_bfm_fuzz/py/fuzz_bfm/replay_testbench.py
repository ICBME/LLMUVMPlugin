import pyuvm
from fuzz_uvm.replay import LibAflUvmReplayTest


@pyuvm.test()
class LibAflBfmReplayTest(LibAflUvmReplayTest):
    """Compatibility entry point for the reusable LibAFL pyUVM replay env."""
