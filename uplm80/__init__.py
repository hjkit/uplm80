"""
uplm80 - Highly optimizing PL/M-80 compiler targeting 8080/Z80

This compiler implements the PL/M-80 language as specified in Intel's
PL/M-80 Programming Manual (9800268B, Jan 1980). It generates optimized
assembly code for either the Intel 8080 or Zilog Z80 processor.
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("uplm80")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"  # Running from source without install
