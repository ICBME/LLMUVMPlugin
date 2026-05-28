"""IR-driven BFM infrastructure for RTLAgent experiments."""

from rtlagent_bfm.ir import (
    DesignIR,
    FieldIR,
    InterfaceIR,
    RegisterIR,
    SignalBindingIR,
    SourceArtifactIR,
)
from rtlagent_bfm.loader import load_ir
from rtlagent_bfm.resolver import ResolvedDesign, SignalResolutionError
from rtlagent_bfm.runtime import BfmRuntimeContext, GeneratedBfmBase

__all__ = [
    "BfmRuntimeContext",
    "DesignIR",
    "FieldIR",
    "GeneratedBfmBase",
    "InterfaceIR",
    "RegisterIR",
    "ResolvedDesign",
    "SignalBindingIR",
    "SignalResolutionError",
    "SourceArtifactIR",
    "load_ir",
]
