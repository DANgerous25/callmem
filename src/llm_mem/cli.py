"""Compatibility shim for ``llm_mem.cli`` — redirects to ``callmem.cli``."""

from callmem.cli import main

if __name__ == "__main__":
    main()
