#!/bin/bash -x
export DOCKER_DEFAULT_PLATFORM=linux/amd64
version="20-may-2024-17.28"
docker build -t shapeblock/sb-operator:${version} .
docker push shapeblock/sb-operator:${version}
