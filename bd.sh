#!/bin/bash -x
version="1.0"
docker build -t shapeblock/sb-operator:${version} .
docker push shapeblock/sb-operator:${version}
# sed 's/__VERSION__/'"$version"'/g' deployment.yaml > /tmp/deployment.yaml
# kubectl apply -f /tmp/deployment.yaml
# kubectl wait --for=condition=ready pod -l application=sb-operator
# kubectl logs -f -l application=sb-operator
