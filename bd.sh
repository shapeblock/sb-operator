#!/bin/bash -x
export DOCKER_DEFAULT_PLATFORM=linux/amd64
version="08-may-2024-17.49"
docker build -t shapeblock/sb-operator:${version} .
docker push shapeblock/sb-operator:${version}
