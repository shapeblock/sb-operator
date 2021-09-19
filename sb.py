import os

import kopf
from kubernetes import client
import yaml
from kubernetes.client.rest import ApiException


@kopf.on.create('projects')
def create_project(spec, name, logger, **kwargs):
    logger.info(f"A project is created with spec: {spec}")
    logger.info(f"Create a namespace")
    create_namespace(name)
    logger.info(f"Create registry credentials")
    create_registry_credentials(name)
    logger.info(f"Create a service account and attach secrets to that")
    create_service_account(name)

def create_namespace(name):
     core_v1 = client.CoreV1Api()
     labels = {"from": "shapeblock"}
     body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name, labels=labels))
     core_v1.create_namespace(body=body)

def delete_namespace(name):
    core_v1 = client.CoreV1Api()
    core_v1.delete_namespace(name=name)


def create_registry_credentials(namespace):
    core_v1 = client.CoreV1Api()
    registry_creds = core_v1.read_namespaced_secret(namespace='default', name='registry-creds')
    body  = client.V1Secret(metadata=client.V1ObjectMeta(name='registry-creds'))
    body.data = registry_creds.data
    body.type = registry_creds.type
    core_v1.create_namespaced_secret(body=body, namespace=namespace)

def create_service_account(namespace):
    core_v1 = client.CoreV1Api()
    body  = client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=namespace))
    body.secrets = [{'name': 'registry-creds'}]
    body.image_pull_secrets = [{'name': 'registry-creds'}]
    core_v1.create_namespaced_service_account(body=body, namespace=namespace)

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
def create_build(spec, status, name, namespace, logger, **kwargs):
    logger.info(f"Create handler for build with status: {status}")
    if status.get('type') == 'Succeeded' and status.get('status') == 'True':
        tag = spec.get('tags')[1]
        logger.info(f"New image created: {tag}")
        # create helmrelease.
        create_helmrelease(name, namespace, tag, logger)


@kopf.on.field('kpack.io', 'v1alpha1', 'builds', field='status.conditions')
def trigger_helm_release(new, logger, **kwargs):
    logger.info(f"Update handler for build with status: {new}")


@kopf.on.update('applications')
def update_app(spec, name, namespace, logger, **kwargs):
    logger.info(f"An application is updated with spec: {spec}")
    api = client.CustomObjectsApi()
    git_info = spec.get('git')
    ref = git_info.get('ref')
    # patch should update more stuff.
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
        name=name,
    )
    logger.info("Image deleted.")
    response = api.delete_namespaced_custom_object(
        group="kpack.io",
        version="v1alpha1",
        namespace=namespace,
        plural="builders",
        body=client.V1DeleteOptions(),
        name=name,
    )
    logger.info("Builder deleted.")
    # delete all build pods and objects
    # clean up the registry
    # delete helm release objects
    # delete volumes if any
    # send notification



@kopf.on.delete('projects')
def delete_project(spec, name, logger, **kwargs):
    logger.info(f"Deleting the namespace...")
    delete_namespace(name)
    # any other cleanup


@kopf.on.startup()
def startup_fn(logger, **kwargs):
    logger.info("check if helm release, ingress, cert, registry, kpack, nfs are installed.")
    logger.info("send notification to SB.")

# daemon to update kpack base images
# daemon to send status to SB every x hrs

def create_helmrelease(name, namespace, tag, logger):
    api = client.CustomObjectsApi()
    app = api.get_namespaced_custom_object(
        group="helm.fluxcd.io",
        version="v1",
        name=name,
        namespace=namespace,
        plural="helmreleases",
    )
    spec = app.spec
    logger.info("App spec: {spec}.")
    try:
        resource = api.get_namespaced_custom_object(
            group="helm.fluxcd.io",
            version="v1",
            name=name,
            namespace=namespace,
            plural="helmreleases",
        )
        logger.info("Helm Release exists.")
    except ApiException as error:
        if error.status == 404:
            chart_info = spec.get('chart')
            chart_name = chart_info.get('name')
            chart_repo = chart_info.get('repo')
            chart_version = chart_info.get('version')
            chart_values = chart_info.get('values')
            path = os.path.join(os.path.dirname(__file__), 'helmrelease.yaml')
            tmpl = open(path, 'rt').read()
            text = tmpl.format(name=name, 
                            chart_name=chart_name,
                            chart_repo=chart_repo,
                            chart_version=chart_version,
                            )
            data = yaml.safe_load(text)
            data['spec']['chart']['values'] = yaml.safe_load(chart_values)
            data['spec']['chart']['values']['php']['image'] = tag
            response = api.create_namespaced_custom_object(
                group="helm.fluxcd.io",
                version="v1",
                namespace=namespace,
                plural="helmreleases",
                body=data,
            )
            logger.info("Helmrelease created.")
