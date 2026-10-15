#!/usr/bin/env python3
"""编译版加载器：逻辑在同目录 lr_host_start_agent_impl 编译模块内"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lr_host_start_agent_impl import main
if __name__ == "__main__":
    raise SystemExit(main())
