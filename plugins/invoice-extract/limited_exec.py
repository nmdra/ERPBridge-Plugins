"""Apply Linux resource limits, then replace this process with a Poppler tool."""
import os
from pathlib import Path
import resource
import sys


def main():
    if len(sys.argv) < 2 or Path(sys.argv[1]).name not in {"pdfinfo", "pdftotext"}:
        raise SystemExit(2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.execv(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
