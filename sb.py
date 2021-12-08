import os

import requests
import kopf
from kubernetes import client
import yaml
from kubernetes.client.rest import ApiException

sb_url = os.getenv('SB_URL')
cluster_id = os.getenv('CLUSTER_ID')

@kopf.on.create('projects')
def create_project(spec, name, labels, logger, **kwargs):
    # TODO: add label
    # TODO: check if project already exists
    project_uuid = labels['shapeblock.com/project-uuid']
    logger.info(f"A project is created with spec: {spec}")
    logger.info(f"Create a namespace")
    create_namespace(name)
    logger.info(f"Create registry credentials")
    create_registry_credentials(name)
    logger.info(f"Create a service account and attach secrets to that")
    create_service_account(name)
    create_role_binding(name)
    core_v1 = client.CoreV1Api()
    resp = core_v1.read_namespaced_service_account(namespace=name, name=name)
    logger.info(resp)
    for secret in resp.secrets:
        if f'{name}-token' in secret.name:
            secret_response = core_v1.read_namespaced_secret(namespace=name, name=secret.name)
            response = requests.post(f"{sb_url}/projects/{project_uuid}/token/", json=secret_response.data)
            logger.info(f"Sent service account token for project {name}.")


def is_valid_project(sb_id):
    response = requests.get(sb_url + '/verify-project/' + sb_id)
    return response.status_code == 200

def create_namespace(name):
    # TODO: check if namespace already exists
     core_v1 = client.CoreV1Api()
     labels = {"from": "shapeblock"}
     body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name, labels=labels))
     core_v1.create_namespace(body=body)

def create_role_binding(namespace):
     rbac_v1 = client.RbacAuthorizationV1Api()
     path = os.path.join(os.path.dirname(__file__), 'role-binding.yaml')
     tmpl = open(path, 'rt').read()
     text = tmpl.format(name=namespace)
     body = yaml.safe_load(text)
     rbac_v1.create_namespaced_role_binding(body=body, namespace=namespace)



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
    # TODO: add label
    core_v1 = client.CoreV1Api()
    body  = client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=namespace))
    body.secrets = [{'name': 'registry-creds'}]
    body.image_pull_secrets = [{'name': 'registry-creds'}]
    core_v1.create_namespaced_service_account(body=body, namespace=namespace)

@kopf.on.create('applications')
def create_app(spec, name, namespace, logger, **kwargs):
    # TODO: add label
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
            chart_info = spec.get('chart')
            stack = chart_info.get('name')
            path = os.path.join(os.path.dirname(__file__), f'builder-{stack}.yaml')
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

@kopf.on.update('kpack.io', 'v1alpha1', 'builds')
def update_build(spec, status, name, namespace, logger, labels, **kwargs):
    logger.info(f'------------------ {name}')
    if status.get('stepsCompleted') == ['prepare']:
        logger.info('POSTing build pod info.')
        data = {
            'pod': status['podName'],
            'name': labels['image.kpack.io/image'],
            'namespace': namespace,
        }
        response = requests.post(f"{sb_url}/build-pod/", json=data)
        logger.info(f"Update handler for build with status: {status}")


@kopf.on.field('kpack.io', 'v1alpha1', 'builds', field='status.conditions')
def trigger_helm_release(name, namespace, labels, spec, status, new, logger, **kwargs):
    logger.info(f"Update handler for build with status: {status}")
    status = new[0]
    if status.get('type') == 'Succeeded' and status.get('status') == 'True':
        tag = spec.get('tags')[1]
        logger.info(f"New image created: {tag}")
        app_name = labels['image.kpack.io/image']
        logger.info(f"Deploying app: {app_name}")
        # create helmrelease.
        create_helmrelease(app_name, namespace, tag, logger)


@kopf.on.update('applications')
def update_app(spec, name, namespace, logger, **kwargs):
    logger.info(f"An application is updated with spec: {spec}")
    api = client.CustomObjectsApi()
    git_info = spec.get('git')
    ref = git_info.get('ref')
    # TODO: patch should update more stuff.
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
    print(response)
    logger.info("Image patched.")

@kopf.on.field('helm.fluxcd.io', 'v1', 'helmreleases', field='status')
def notify_helm_release(name, namespace, labels, spec, status, new, old, logger, **kwargs):
    if old['releaseStatus'] == 'pending-upgrade' and new['releaseStatus'] == 'deployed':
        logger.info(f'------------------ new: {new}')
        logger.info('POSTing helm release status.')
        data = {
            'name': name,
            'namespace': namespace,
        }
        response = requests.post(f"{sb_url}/helm-status/", json=data)
        logger.info(f"Updates Helm release status for {name} successfully.")

    #TODO: update status of helm release

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
    # TODO: delete all build pods and objects
    # TODO: clean up the registry
    # TODO: delete helm release objects
    # TODO: delete volumes if any
    # TODO: send notification



@kopf.on.delete('projects')
def delete_project(spec, name, logger, **kwargs):
    logger.info(f"Deleting the namespace...")
    delete_namespace(name)
    # any other cleanup
    # TODO: send notification


@kopf.on.startup()
def startup_fn(logger, **kwargs):
    logger.info("check if helm release, ingress, cert, registry, kpack, nfs are installed.")
    logger.info("send notification to SB.")
    send_cluster_admin_account(logger)

def send_cluster_admin_account(logger):
    data ={
        'token': open('/var/run/secrets/kubernetes.io/serviceaccount/token').read(),
        'ca.crt': open('/var/run/secrets/kubernetes.io/serviceaccount/ca.crt').read(),
    }
    logger.info('POSTing token info.')
    response = requests.post(f"{sb_url}/clusters/{cluster_id}/token-info", json=data)
    if response.status_code == 202:
        logger.info("POSTed token info.")


# TODO: daemon to update kpack base images
# TODO: daemon to send status to SB every x hrs

def create_helmrelease(name, namespace, tag, logger):
    # TODO: add label
    api = client.CustomObjectsApi()
    app = api.get_namespaced_custom_object(
        group="dev.shapeblock.com",
        version="v1alpha1",
        name=name,
        namespace=namespace,
        plural="applications",
    )
    spec = app.get('spec')
    logger.info(f"App spec: {spec}.")
    try:
        resource = api.get_namespaced_custom_object(
            group="helm.fluxcd.io",
            version="v1",
            name=name,
            namespace=namespace,
            plural="helmreleases",
        )
        logger.info("Helm Release exists, patching ...")
        chart_info = spec.get('chart')
        chart_values = chart_info.get('values')
        data = {
            "spec": {
                "values": yaml.safe_load(chart_values),
            }
        }
        stack = chart_info.get('name')
        if stack == 'drupal':
            data['spec']['values']['php']['image'] = tag
        if stack == 'nodejs':
            data['spec']['values']['image']['repository'] = tag
        response = api.patch_namespaced_custom_object(
            group="helm.fluxcd.io",
            version="v1",
            namespace=namespace,
            name=name,
            plural="helmreleases",
            body=data,
        )
        logger.info("Helmrelease patched.")
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
            data['spec']['values'] = yaml.safe_load(chart_values)
            stack = chart_name
            if stack == 'drupal':
                data['spec']['values']['php']['image'] = tag
            if stack == 'nodejs':
                data['spec']['values']['image']['repository'] = tag
            response = api.create_namespaced_custom_object(
                group="helm.fluxcd.io",
                version="v1",
                namespace=namespace,
                plural="helmreleases",
                body=data,
            )
            logger.info("Helmrelease created.")
