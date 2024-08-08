#!/bin/bash -x
export DOCKER_DEFAULT_PLATFORM=linux/amd64
version="07-aug-2024-158.30"
docker build -t shapeblock/sb-operator:${version} .
docker push shapeblock/sb-operator:${version}
