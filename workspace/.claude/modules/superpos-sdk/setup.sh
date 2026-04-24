#!/bin/bash
# Clone (or update) the Superpos SDK repo into vendor/ and install the Python package.
# Runs at container startup from module_setup.py. Idempotent.

set -e

REPO_URL="${SUPERPOS_SDK_REPO:-https://github.com/Superpos-AI/superpos-sdk}"
REF="${SUPERPOS_SDK_REF:-main}"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENDOR_DIR="$MODULE_DIR/vendor"

if [ ! -d "$VENDOR_DIR/.git" ]; then
    echo "superpos-sdk: cloning $REPO_URL into vendor/"
    git clone --depth 1 --branch "$REF" "$REPO_URL" "$VENDOR_DIR"
else
    echo "superpos-sdk: updating vendor/ (fetch $REF)"
    git -C "$VENDOR_DIR" fetch --depth 1 origin "$REF"
    git -C "$VENDOR_DIR" checkout "$REF"
    git -C "$VENDOR_DIR" reset --hard "origin/$REF"
fi

echo "superpos-sdk: installing Python package"
python3 -m pip install --user --break-system-packages -q -e "$VENDOR_DIR/sdk/python"
