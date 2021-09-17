#!/bin/bash -x
version="0.0.9"
docker build --file Dockerfile.kubectl -t shapeblock/sb-operator-init:${version} .
docker push shapeblock/sb-operator-init:${version}
