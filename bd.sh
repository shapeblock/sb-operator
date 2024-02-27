#!/bin/bash -x
export DOCKER_DEFAULT_PLATFORM=linux/amd64
version="27-feb-2024-13.59"
docker build -t shapeblock/sb-operator:${version} .
docker push shapeblock/sb-operator:${version}
