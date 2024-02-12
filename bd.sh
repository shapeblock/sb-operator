#!/bin/bash -x
export DOCKER_DEFAULT_PLATFORM=linux/amd64
version="12-feb-2024-18.43"
docker build -t shapeblock/sb-operator:${version} .
docker push shapeblock/sb-operator:${version}
