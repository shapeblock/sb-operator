#!/bin/bash -x
export DOCKER_DEFAULT_PLATFORM=linux/amd64
version="15-july-2024-15.37"
docker build -t shapeblock/sb-operator:${version} .
docker push shapeblock/sb-operator:${version}
