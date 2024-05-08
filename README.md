# SB operator

## Deployment steps

1. Apply the application and other CRDs in the cluster.
2. Create SB admin with cluster-admin role.
3. [optional] create new namespace.
4. Deploy the operator.

## TODO

1. Project CRD - done
2. Project artefacts creation - done
3. Helm release reconcile - done
4. Send notifications to SB.
    a. build pod name
    b. image build status
    c. helm deployment status
    d. project creation status
    e. app creation status
    f. ingress name and ip
    g. managed kubernetes provider
    h. sb-admin service account details
5. Clean up stuff on app delete - done
6. Clean up stuff on project delete - done
8. Add appropriate labels to all the artefacts.
9. better exception handling

## TODO 2

1. daemon to update images.
2. deeper clean
3. compile to binary
4. Clean up stuff on uninstall SB.

## TODO 3

1. resize registry
2. resize nfs
3. upgrade versions
4. add ssh to app
5. add registry management credentials to cluster

## TODO 4

1. Custom build templates
2. Other build techniques apart from CNB

## Architecture

1. Installation of helm release, ingress, cert, registry, kpack, nfs using init container.
2. The parameters for this will be controlled by env vars to the init container.
3. Boot the actual operator pod which will be a shiv compiled binary.

## Ideal deployment

Roll up the following:
1. application and project CRDs.
2. SB admin sa with cluster-admin role.
3. Create a new SB namespace.
4. Operator deployment.

into a single file.

Deploy the file on a fresh cluster.

```
echo "eB?rtK>j@iLd_U+5Gy9=<<P52G3" | htpasswd -bnBC 10 "" - | tr -d ':\n' # REGISTRY_PASSWORD
echo "little-bird-ae3c-admin:eB?rtK>j@iLd_U+5Gy9=<<P52G3" | base64 # REGISTRY_CREDENTIALS
```



```
docker build --file Dockerfile.kubectl -t shapeblock/sb-operator-init:0.0.1 .
docker push shapeblock/sb-operator-init:0.0.1
```

# TODO 27th March 2024
- kopf adopt for app dependent resources
- tests
- add probe
- update deployment image to 3.12
- check RBAC - https://kopf.readthedocs.io/en/stable/deployment/#rbac
- idempotence - https://kopf.readthedocs.io/en/stable/idempotence/

