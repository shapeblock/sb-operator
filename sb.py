import os

import kopf
from kubernetes import client
import yaml
from kubernetes.client.rest import ApiException

@kopf.on.create('applications')
def create_app(spec, name, namespace, logger, **kwargs):
    logger.info(f"An application is created with spec: {spec}")
    api = client.CustomObjectsApi()
    # Create builder
    try:
        resource = api.get_namespaced_custom_object(
            group="kpack.io",
            version="v1alpha1",
            name=name,
            namespace=namespace,
            plural="builders",
        )
        logger.info("Builder exists.")
    except ApiException as error:
        if error.status == 404:
            tag = spec.get('tag')
            path = os.path.join(os.path.dirname(__file__), 'builder.yaml')
            tmpl = open(path, 'rt').read()
            text = tmpl.format(name=name, tag=tag, service_account=namespace)
            data = yaml.safe_load(text)
            response = api.create_namespaced_custom_object(
                group="kpack.io",
                version="v1alpha1",
                namespace=namespace,
                plural="builders",
                body=data,
            )
            logger.info("Builder created.")

    # create image
    try:
        resource = api.get_namespaced_custom_object(
            group="kpack.io",
            version="v1alpha1",
            name=name,
            namespace=namespace,
            plural="images",
        )
        logger.info("Image exists.")
    except ApiException as error:
        if error.status == 404:
            tag = spec.get('tag')
            git_info = spec.get('git')
            repo = git_info.get('repo')
            ref = git_info.get('ref')
            service_account = namespace
            builder = name
            path = os.path.join(os.path.dirname(__file__), 'image.yaml')
            tmpl = open(path, 'rt').read()
            text = tmpl.format(name=name, tag=tag, service_account=service_account, repo=repo, ref=ref, builder_name=builder)
            data = yaml.safe_load(text)
            response = api.create_namespaced_custom_object(
                group="kpack.io",
                version="v1alpha1",
                namespace=namespace,
                plural="images",
                body=data,
            )
            logger.info("Image created.")

@kopf.on.create('kpack.io', 'v1alpha1', 'builds')
def update_build(spec, status, namespace, logger, **kwargs):
    logger.info(f"Create handler for build with status: {status}")
    # create helmrelease.


@kopf.on.field('kpack.io', 'v1alpha1', 'builds', field='status.conditions')
def trigger_helm_release(old, new, logger, **kwargs):
    logger.info(f"Update handler for build with status: {new}")


@kopf.on.update('applications')
def update_app(spec, name, namespace, logger, **kwargs):
    logger.info(f"An application is updated with spec: {spec}")
    api = client.CustomObjectsApi()
    git_info = spec.get('git')
    ref = git_info.get('ref')
    patch_body = {
        "spec": {
            "source": {
                "git": {
                    "revision": ref,
                }
            }
        }
    }
    response = api.patch_namespaced_custom_object(
        group="kpack.io",
        version="v1alpha1",
        namespace=namespace,
        name=name,
        plural="images",
        body=patch_body,
    )
    logger.info("Image patched.")


# on field update of helmrelease
    # send notification

@kopf.on.delete('applications')
def delete_app(spec, name, namespace, logger, **kwargs):
    logger.info(f"An application is created with spec: {spec}")
    api = client.CustomObjectsApi()
    response = api.delete_namespaced_custom_object(
        group="kpack.io",
        version="v1alpha1",
        namespace=namespace,
        plural="images",
        body=client.V1DeleteOptions(),
    )
    logger.info("Image deleted.")
    response = api.delete_namespaced_custom_object(
        group="kpack.io",
        version="v1alpha1",
        namespace=namespace,
        plural="builders",
        body=client.V1DeleteOptions(),
    )
    logger.info("Builder deleted.")
    # delete all build pods and objects
    # clean up the registry
    # delete helm release objects
    # delete volumes if any
    # send notification

# on create of projects
#   create namespace
#   create roles(??)

# on delete of project

@kopf.on.startup()
def startup_fn(logger, **kwargs):
    logger.info("install helmrelease")
    logger.info("install ingress, cert, registry, kpack")

# daemon to update kpack base images
