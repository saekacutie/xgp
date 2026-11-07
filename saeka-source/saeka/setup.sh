#!/bin/sh
set -eu

# Install the package from the directory containing this script.
cd "$(dirname "$0")"
python -m pip install .
