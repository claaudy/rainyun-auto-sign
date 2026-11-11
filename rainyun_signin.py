#!/usr/bin/env python3
"""Default host entrypoint for the HTTP-only Rainyun client."""
from rainyun_http import main


if __name__ == '__main__':
    raise SystemExit(main())
