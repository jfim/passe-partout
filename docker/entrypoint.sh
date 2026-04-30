#!/usr/bin/env bash
set -euo pipefail

case "${USE_XVFB:-0}" in
    1|true|TRUE|yes|YES)
        export HEADLESS=0
        exec xvfb-run -a --server-args="-screen 0 1280x1024x24" python -m passe_partout
        ;;
    *)
        exec python -m passe_partout
        ;;
esac
