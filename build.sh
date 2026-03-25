#!/usr/bin/env bash
set -e

IMAGE_NAME="${1:-slim-apiary-agent}"

# Always build the base image
docker build -t "${IMAGE_NAME}-base" .

if [ -f Dockerfile.local ]; then
    echo "Found Dockerfile.local — building custom image..."
    docker build -f Dockerfile.local -t "$IMAGE_NAME" .
else
    # No local customization — tag base as final
    docker tag "${IMAGE_NAME}-base" "$IMAGE_NAME"
fi

echo "Built: $IMAGE_NAME"
