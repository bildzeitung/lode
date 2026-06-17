#!/bin/bash -ex
#
# Initialize the Python environment for lode.
#
# Run from the repo root. Builds ./venv and installs requirements.txt.
# Lightweight: just the environment, nothing else.

python -m venv venv
. ./venv/bin/activate \
    && pip install -U uv \
    && uv pip install -r requirements.txt
