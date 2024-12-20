#!/bin/bash -x
export DOCKER_DEFAULT_PLATFORM=linux/amd64
version="20-dec-2024.1"
docker build -t shapeblock/sb-operator:${version} .
docker push shapeblock/sb-operator:${version}
