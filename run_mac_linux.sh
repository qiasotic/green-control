#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then bash setup_mac_linux.sh; fi
.venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
