#!/bin/bash -ex
#
# Pull the third-party tools this repo depends on
#

# Gastown Hall Beads (ticketing system)
curl -sSL https://raw.githubusercontent.com/gastownhall/beads/main/scripts/install.sh | bash

# RTK (reduce token usage)
curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/master/install.sh | sh
