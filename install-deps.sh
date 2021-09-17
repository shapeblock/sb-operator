#!/usr/bin/env bash

#TODO
# 1. Send status after every step to SB.
# 2. Move DNS to SB.
# 3. Have temp files for everything.

# Flux helm operator
kubectl create namespace flux --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f https://raw.githubusercontent.com/fluxcd/helm-operator/v${FLUX_HELM_OPERATOR_VERSION}/deploy/crds.yaml
helm upgrade -i helm-operator fluxcd/helm-operator --version="${FLUX_HELM_OPERATOR_VERSION}" --set helm.versions=v3 --set rbac.create=true -n flux --wait

# nginx ingress
kubectl create namespace ingress-nginx --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install ingress-nginx bitnami/nginx-ingress-controller --version="${NGINX_INGRESS_VERSION}" -n ingress-nginx --wait 

# both these steps should be ideally moved to cloud.
# set sb cloud A record

ingress_ip=$(kubectl get svc nginx-ingress-nginx-ingress-controller -n ingress-nginx -o yaml  | yj | jq -r '.status.loadBalancer.ingress[0].ip')
generate_post_data()
{
cat <<EOF
{
    "name": "$CLUSTER_NAME",
    "type": "A",
    "content": "$ingress_ip"
}
EOF
}
curl  -H 'Authorization: Bearer TLK9gsTiCT2LuDTwABaiDXzUuA2BTgls' \
        -H 'Accept: application/json' \
        -H 'Content-Type: application/json' \
        -X POST \
        https://api.dnsimple.com/v2/88397/zones/shapeblock.cloud/records \
        --data-raw "$(generate_post_data)"


# set sb cloud CNAME record

generate_post_data_cname()
{
cat <<EOF
{
    "name": "*.$CLUSTER_NAME",
    "type": "CNAME",
    "content": "$CLUSTER_NAME.shapeblock.cloud"
}
EOF
}
curl  -H 'Authorization: Bearer TLK9gsTiCT2LuDTwABaiDXzUuA2BTgls' \
        -H 'Accept: application/json' \
        -H 'Content-Type: application/json' \
        -X POST \
        https://api.dnsimple.com/v2/88397/zones/shapeblock.cloud/records \
        --data-raw "$(generate_post_data_cname)"


# cert manager
kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install cert-manager jetstack/cert-manager --version="${CERT_MANAGER_VERSION}" --set installCRDs=true -n cert-manager --wait

# cert issuer
cat > cert-issuer.yml << EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
    name: letsencrypt-prod
spec:
    acme:
    email: $LETSENCRYPT_EMAIL
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
        name: letsencrypt-secret-prod
    solvers:
    - http01:
        ingress:
            class: nginx
EOF
kubectl apply -f cert-issuer.yml

# kpack
kubectl apply -f https://github.com/pivotal/kpack/releases/download/v${KPACK_VERSION}/release-${KPACK_VERSION}.yaml

# cluster stack
cat > stack.yml <<EOF
apiVersion: kpack.io/v1alpha1
kind: ClusterStack
metadata:
    name: base
spec:
    id: "io.buildpacks.stacks.bionic"
    buildImage:
    image: "paketobuildpacks/build:full-cnb"
    runImage:
    image: "paketobuildpacks/run:full-cnb"
EOF
kubectl apply -f stack.yml

# cluster store
cat > store.yml <<EOF
apiVersion: kpack.io/v1alpha1
kind: ClusterStore
metadata:
    name: default
spec:
    sources:
    - image: gcr.io/paketo-buildpacks/ca-certificates
    - image: gcr.io/paketo-buildpacks/nginx
    - image: gcr.io/paketo-buildpacks/php-dist
    - image: gcr.io/paketo-buildpacks/php-composer
    - image: gcr.io/paketo-buildpacks/php-web
EOF
kubectl apply -f store.yml

# nfs
cat > /tmp/nfs-values.yml << EOF
persistence:
    enabled: true
    size: $NFS_SIZE
EOF

helm upgrade --install nfs-server nfs-server-provisioner  --version="${NFS_VERSION}" --values=/tmp/nfs-values.yml -n default --wait

# registry
bcrypt='$REGISTRY_PASSWORD'
cat > /tmp/registry-values.yml << EOF
persistence:
    enabled: true
    size: $REGISTRY_SIZE
ingress:
    enabled: true
    hosts:
    - $REGISTRY_URL
    tls:
    - secretName: registry-tls
        hosts:
        - $REGISTRY_URL
    annotations:
    kubernetes.io/ingress.class: nginx
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/proxy-body-size: 0
secrets:
    htpasswd: "$REGISTRY_USERNAME:$bcrypt"
EOF
helm upgrade --install docker-registry docker-registry --version="${CONTAINER_REGISTRY_VERSION}" --values=/tmp/registry-values.yml -n default --wait

# add registry creds
cat > /tmp/dockerconfig.json << EOF
{
    "auths": {
        "$REGISTRY_URL": {
            "auth": "$REGISTRY_CREDENTIALS"
        }
    }
}
EOF
kubectl get secret registry-creds -n default || kubectl create secret generic registry-creds --from-file=.dockerconfigjson=/tmp/dockerconfig.json --type=kubernetes.io/dockerconfigjson -n default
