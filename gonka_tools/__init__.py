"""
Gonka.ai Toolchain
==================

A comprehensive toolkit for managing Gonka.ai compute nodes, monitoring their health,
and analyzing mining performance.

Modules:
    - setup: Remote server setup and node deployment
    - monitor: Health monitoring with Telegram notifications
    - analytics: Mining performance analysis and reporting
    - cli: Command-line interface for all operations
"""

__version__ = "0.1.0"
__author__ = "Gonka Tools"

from .config import Settings, load_nodes_config

__all__ = ["Settings", "load_nodes_config", "__version__"]

