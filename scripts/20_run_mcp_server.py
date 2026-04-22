#!/usr/bin/env python3
"""
Run the FastMCP server for lua_callgraph_propagation_agent.

Typical usage:

  ./lua_llm/bin/python scripts/20_run_mcp_server.py
"""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lua_callgraph_propagation_agent.mcp_server import main


if __name__ == "__main__":
    main()
