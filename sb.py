import os
import time
import datetime
from typing import Dict
import requests
import kopf
from kubernetes import client, config
import yaml
from kubernetes.client.rest import ApiException
import pusher

pusher_client = pusher.Pusher(
  app_id='493518',
  key='0d54c047764d470474af',
  secret='b6d607c0af8ea2b66b4f',
  cluster='mt1',
  ssl=True
)

def trigger_chunked(pusher_client, app_uuid, event, data):
    chunk_size = 9000
    i = 0
    logs = data.pop('logs')

    while True:
        iteration = i*chunk_size
        data['index'] = i
        data['chunk'] = logs[iteration:iteration + chunk_size],
        data['final'] = chunk_size*(i+1) >= len(logs)
        if len(logs[iteration:iteration + chunk_size]):
            pusher_client.trigger(str(app_uuid), event, data)
        if i*chunk_size > len(logs):
            break
        i=i+1


def get_sb_url(sb_url):
    if sb_url.startswith('https://'):
        return sb_url
    return f'https://{sb_url}'

sb_url = get_sb_url(os.getenv('SB_URL'))
cluster_id = os.getenv('CLUSTER_ID')


@kopf.on.create('projects')
def create_project(spec, name, labels, logger, **kwargs):
    # TODO: check if project already exists
    project_uuid = labels['shapeblock.com/project-uuid']
    logger.debug(f"A project is created with spec: {spec}")
    logger.info(f"Create a namespace")
    create_namespace(name)
    logger.info(f"Create registry credentials")
    create_registry_credentials(name)
    logger.info(f"Create a service account and attach secrets to that")
    create_service_account(name, name, logger)
    create_role_binding(name)
    core_v1 = client.CoreV1Api()
    resp = core_v1.read_namespaced_service_account(namespace=name, name=name)
    logger.debug(resp)
    for secret in resp.secrets:
        if f'{name}-token' in secret.name:
            secret_response = core_v1.read_namespaced_secret(namespace=name, name=secret.name)
            response = requests.post(f"{sb_url}/projects/{project_uuid}/token/", json=secret_response.data, verify=False)
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

def create_service_account(name, namespace, logger):
    # TODO: add label
    logger.info(f"Creating service account {name} in {namespace}")
    core_v1 = client.CoreV1Api()
    body  = client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=name))
    body.secrets = [{'name': 'registry-creds'}]
    body.image_pull_secrets = [{'name': 'registry-creds'}]
    service_account = core_v1.create_namespaced_service_account(body=body, namespace=namespace)
    time.sleep(4) # to wait till a secret gets attached to the SA
    return service_account

@kopf.on.create('applications')
def create_app(spec, name, labels, namespace, logger, **kwargs):
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return
    logger.debug(f"An application is created with spec: {spec}")
    api = client.CustomObjectsApi()
    # create service account
    #TODO: handle exception if already created
    try:
        service_account = create_service_account(name, namespace, logger)
    except:
        logger.debug("Serivce account already exists.")
    # add secret to service account if private repo
    git_info = spec.get('git')
    repo = git_info.get('repo')
    # TODO: This is not a hard enough check.
    if repo.startswith('git@'):
        logger.info('Attaching ssh secret.')
        core_v1 = client.CoreV1Api()
        ssh_secret = client.V1ObjectReference(kind='Secret', name=f'{name}-ssh')
        service_account.secrets.append(ssh_secret)
        try:
            service_account = core_v1.patch_namespaced_service_account(namespace=namespace, name=name, body=client.V1ServiceAccount(secrets=service_account.secrets))
        except:
            logger.error(f'??? Unable to update service account for app {name} in project {namespace}.')
            return
    response = requests.get(f"{sb_url}/apps/{app_uuid}/last-deployment/")
    if response.status_code == 200:
        deployment_uuid = response.json()['deployment']
    else:
        deployment_uuid = None
    # Create builder
    try:
        resource = api.get_namespaced_custom_object(
            group="kpack.io",
            version="v1alpha2",
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
            text = tmpl.format(name=name, tag=tag, service_account=name, app_uuid=app_uuid)
            data = yaml.safe_load(text)
            response = api.create_namespaced_custom_object(
                group="kpack.io",
                version="v1alpha2",
                namespace=namespace,
                plural="builders",
                body=data,
            )
            logger.info("Builder created.")
            data = {
            'logs': 'Builder created.\n',
            'status': 'running',
            'app_uuid': app_uuid,
            }
            if deployment_uuid:
                data['deployment_uuid'] = deployment_uuid
            pusher_client.trigger(str(app_uuid), 'deployment', data)
            response = requests.post(f"{sb_url}/deployments/", json=data, verify=False)
            #response = requests.post(f"{sb_url}/helm-status/", json=data)

    # create image
    try:
        resource = api.get_namespaced_custom_object(
            group="kpack.io",
            version="v1alpha2",
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
            service_account = name
            builder = name
            path = os.path.join(os.path.dirname(__file__), 'image.yaml')
            tmpl = open(path, 'rt').read()
            text = tmpl.format(name=name, tag=tag, service_account=service_account, repo=repo, ref=ref, builder_name=builder, app_uuid=app_uuid)
            data = yaml.safe_load(text)
            chart_info = spec.get('chart')
            build_envs = chart_info.get('build')
            if build_envs:
                data['spec']['build'] = {'env' : build_envs}
            response = api.create_namespaced_custom_object(
                group="kpack.io",
                version="v1alpha2",
                namespace=namespace,
                plural="images",
                body=data,
            )
            logger.info("Image created.")
            data = {
            'logs': 'Image created.\n',
            'app_uuid': app_uuid,
            'status': 'running',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
            }
            pusher_client.trigger(str(app_uuid), 'deployment', data)
            response = requests.post(f"{sb_url}/deployments/", json=data, verify=False)
    return {'lastDeployment': deployment_uuid}

@kopf.on.update('kpack.io', 'v1alpha2', 'builds')
def update_build(spec, status, name, namespace, logger, labels, **kwargs):
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return
    app_name = labels['image.kpack.io/image']
    app_status = get_app_status(namespace, app_name, logger)
    if 'update_app' in app_status.keys():
        deployment_uuid = app_status['update_app'].get('lastDeployment')
    else:
        deployment_uuid = app_status['create_app'].get('lastDeployment')
    core_v1 = client.CoreV1Api()
    data = {
            'app_uuid': app_uuid,
            'status': 'running',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
    }
    steps_completed = status.get('stepsCompleted')
    if steps_completed:
        data['logs'] = core_v1.read_namespaced_pod_log(namespace=namespace, name=status['podName'], container=steps_completed[-1])
        if len(data['logs']) > 9000:
            trigger_chunked(pusher_client, str(app_uuid), 'chunked-deployment', dict(data))
        else:
            if len(data['logs']):
                pusher_client.trigger(str(app_uuid), 'deployment', data)
        response = requests.post(f"{sb_url}/deployments/", json=data, verify=False)

@kopf.on.field('kpack.io', 'v1alpha2', 'builds', field='status.conditions')
def trigger_helm_release(name, namespace, labels, spec, status, new, logger, **kwargs):
    logger.info(f"Update handler for build with status: {status}")
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return
    app_name = labels['image.kpack.io/image']
    app_status = get_app_status(namespace, app_name, logger)
    if 'update_app' in app_status.keys():
        deployment_uuid = app_status['update_app'].get('lastDeployment')
    else:
        deployment_uuid = app_status['create_app'].get('lastDeployment')
    status = new[0]
    if status.get('type') == 'Succeeded' and status.get('status') == 'True':
        tag = spec.get('tags')[1]
        logger.info(f"New image created: {tag}")
        logger.info(f"Deploying app: {app_name}")
        # create helmrelease.
        data = {
            'logs': 'Triggering Helm release.\n',
            'status': 'running',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
        }
        pusher_client.trigger(str(app_uuid), 'deployment', data)
        response = requests.post(f"{sb_url}/deployments/", json=data, verify=False)
        create_helmrelease(app_name, app_uuid, namespace, tag, logger)
        # Update the latest image tag in app status
        update_app_status(namespace, app_name, tag, logger)
    if status.get('type') == 'Succeeded' and status.get('status') == 'False':
        data = {
            'logs': 'Build stage failed.\n',
            'status': 'failed',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
        }
        pusher_client.trigger(str(app_uuid), 'deployment', data)
        response = requests.post(f"{sb_url}/deployments/", json=data, verify=False)

def get_last_tag(status: Dict):
    """
    Get last deployed tag from application status.
    """
    return status['lastTag']

@kopf.on.update('applications')
def update_app(spec, name, namespace, logger, labels, status, **kwargs):
    logger.debug(f"An application is updated with spec: {spec}")
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return
    response = requests.get(f"{sb_url}/apps/{app_uuid}/last-deployment/")
    if response.status_code == 200:
        deployment_uuid = response.json()['deployment']
        deployment_type = response.json()['type']
        # if app scale, then skip building and trigger a helm release directly.
        if deployment_type == 'scale':
            tag = get_last_tag(status)
            # update app status with last deployment
            update_app_deployment_status(namespace, name, deployment_uuid, logger)
            create_helmrelease(name, app_uuid, namespace, tag, logger)
            return
    api = client.CustomObjectsApi()
    git_info = spec.get('git')
    ref = git_info.get('ref')
    chart_info = spec.get('chart')
    build_envs = chart_info.get('build')
    # TODO: Create image and build if it doesn't exist already
    # If config change, add a build env var. It is harmless and triggers a new build.
    patch_body = {
        "spec": {
            "source": {
                "git": {
                    "revision": ref,
                }
            },
            "build": {
                "env": build_envs,
            }
        }
    }
    build_ts = {
                "name": "SB_TS",
                "value": str(datetime.datetime.now()),
    }
    patch_body['spec']['build']['env'].append(build_ts)
    logger.debug(patch_body)
    response = api.patch_namespaced_custom_object(
        group="kpack.io",
        version="v1alpha2",
        namespace=namespace,
        name=name,
        plural="images",
        body=patch_body,
    )
    logger.info("Image patched.")
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return
    data = {
        'logs': 'Patched image.\n',
        'status': 'running',
        'app_uuid': app_uuid,
        'deployment_uuid': deployment_uuid,
    }
    pusher_client.trigger(str(app_uuid), 'deployment', data)
    response = requests.post(f"{sb_url}/deployments/", json=data, verify=False)
    return {'lastDeployment': deployment_uuid}


@kopf.on.field('helm.toolkit.fluxcd.io', 'v2beta1', 'helmreleases', field='status.observedGeneration')
def notify_helm_release(old, new, labels, diff, namespace, name, logger, **kwargs):
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return
    logger.info('POSTing helm release status.')
    app_status = get_app_status(namespace, name, logger)
    if 'update_app' in app_status.keys():
        deployment_uuid = app_status['update_app'].get('lastDeployment')
    else:
        deployment_uuid = app_status['create_app'].get('lastDeployment')
    #TODO: check if helm status is successful.
    data = {
        'logs': 'Notify helm release.\n',
        'status': 'success',
        'app_uuid': app_uuid,
        'deployment_uuid': deployment_uuid,
    }
    pusher_client.trigger(str(app_uuid), 'deployment', data)
    logger.info(f"Updated Helm release status for {name} successfully.")
    response = requests.post(f"{sb_url}/deployments/", json=data, verify=False)


@kopf.on.delete('applications')
def delete_app(spec, name, namespace, logger, **kwargs):
    logger.debug(f"An application is deleted with spec: {spec}")
    api = client.CustomObjectsApi()
    try:
        response = api.delete_namespaced_custom_object(
            group="kpack.io",
            version="v1alpha2",
            namespace=namespace,
            plural="images",
            body=client.V1DeleteOptions(),
            name=name,
        )
        logger.info("Image deleted.")
    except:
        logger.info('Unable to delete image.')
    try:
        response = api.delete_namespaced_custom_object(
            group="kpack.io",
            version="v1alpha2",
            namespace=namespace,
            plural="builders",
            body=client.V1DeleteOptions(),
            name=name,
        )
        logger.info("Builder deleted.")
    except:
        logger.info('Unable to delete builder.')
    # TODO: clean up the registry
    # Delete helm release objects
    # This will delete all build pods and objects.
    try:
        response = api.delete_namespaced_custom_object(
            group="helm.toolkit.fluxcd.io",
            version="v2beta1",
            name=name,
            namespace=namespace,
            plural="helmreleases",
            body=client.V1DeleteOptions(),
        )
        logger.info("Helm release deleted.")
    except:
        logger.info('Unable to delete helm release.')
    # Delete any job
    # Delete ingress tls secret
    # Delete volumes if any
    core_v1 = client.CoreV1Api()
    label = f"app.kubernetes.io/instance={namespace}-{name}"
    pvcs = core_v1.list_namespaced_persistent_volume_claim(namespace=namespace, label_selector=label)
    for pvc in pvcs.items:
        resp = core_v1.delete_namespaced_persistent_volume_claim(namespace=namespace, body=client.V1DeleteOptions(), name=pvc.metadata.name)
        logger.info(f'Deleting PVC {pvc.metadata.name}')
    logger.info("volumes deleted.")

    # delete secret
    try:
        logger.info('Deleting secrets.')
        core_v1.delete_namespaced_secret(namespace=namespace, name=f'{name}-ssh')
    except:
        logger.info(f'Unable to delete secret.')
    # Delete service account
    logger.info('Deleting service account.')
    core_v1.delete_namespaced_service_account(namespace=namespace, name=name)

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
    response = requests.post(f"{sb_url}/clusters/{cluster_id}/token-info", json=data, verify=False)
    if response.status_code == 202:
        logger.info("POSTed token info.")
    nodes = get_nodes_info()
    response = requests.post(f"{sb_url}/clusters/{cluster_id}/nodes", json=nodes, verify=False)
    logger.info("POSTing node info")
    logger.info(response.status_code)
    if response.status_code == 201:
        logger.info("POSTed node info.")
    else:
        logger.error('Unable to send node information.')

@kopf.on.cleanup()
async def cleanup_fn(logger, **kwargs):
    nodes = get_nodes_info()
    response = requests.post(f"{sb_url}/clusters/{cluster_id}/nodes-delete", json=nodes, verify=False)
    if response.status_code == 201:
        logger.info("DELETE node info.")

# TODO: daemon to update kpack base images
# TODO: daemon to send status to SB every x hrs

def create_helmrelease(name, app_uuid, namespace, tag, logger):
    # TODO: add label
    api = client.CustomObjectsApi()
    try:
        app = api.get_namespaced_custom_object(
            group="dev.shapeblock.com",
            version="v1alpha1",
            name=name,
            namespace=namespace,
            plural="applications",
        )
    except ApiException as error:
        if error.status == 404:
            logger.error(f"??? Application {name} not found in namespace {namespace}.")
            return
    spec = app.get('spec')
    logger.debug(f"App spec: {spec}.")
    app_status = get_app_status(namespace, name, logger)
    if 'update_app' in app_status.keys():
        deployment_uuid = app_status['update_app'].get('lastDeployment')
    else:
        deployment_uuid = app_status['create_app'].get('lastDeployment')
    try:
        resource = api.get_namespaced_custom_object(
            group="helm.toolkit.fluxcd.io",
            version="v2beta1",
            name=name,
            namespace=namespace,
            plural="helmreleases",
        )
        logger.info("Helm Release exists, patching ...")
        chart_info = spec.get('chart')
        chart_values = chart_info.get('values')
        data = {
            "spec": {
                "chart": {
                    # Add chart version from spec
                    "version": chart_info.get('version'),
                },
                "values": yaml.safe_load(chart_values),
            }
        }
        stack = chart_info.get('name')
        if stack in ['drupal', 'php']:
            data['spec']['values']['php']['image'] = tag
        if stack in ['nodejs', 'django']:
            data['spec']['values']['image']['repository'] = tag
        response = api.patch_namespaced_custom_object(
            group="helm.toolkit.fluxcd.io",
            version="v2beta1",
            namespace=namespace,
            name=name,
            plural="helmreleases",
            body=data,
        )
        logger.info("Helmrelease patched.")
        data = {
            'logs': 'Patched Helm Release.\n',
            'status': 'running',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
        }
        pusher_client.trigger(str(app_uuid), 'deployment', data)
        response = requests.post(f"{sb_url}/deployments/", json=data, verify=False)
    except ApiException as error:
        if error.status == 404:
            chart_info = spec.get('chart')
            chart_name = chart_info.get('name')
            chart_repo = chart_info.get('repo')
            chart_version = chart_info.get('version')
            chart_values = chart_info.get('values')
            path = os.path.join(os.path.dirname(__file__), 'helmrelease2.yaml')
            tmpl = open(path, 'rt').read()
            text = tmpl.format(name=name,
                            chart_name=chart_name,
                            chart_repo=chart_repo,
                            chart_version=chart_version,
                            app_uuid=app_uuid,
                            )
            data = yaml.safe_load(text)
            data['spec']['values'] = yaml.safe_load(chart_values)
            stack = chart_name
            if stack in ['drupal', 'php']:
                data['spec']['values']['php']['image'] = tag
            if stack == 'nodejs':
                data['spec']['values']['image']['repository'] = tag
            response = api.create_namespaced_custom_object(
                group="helm.toolkit.fluxcd.io",
                version="v2beta1",
                namespace=namespace,
                plural="helmreleases",
                body=data,
            )
            logger.info("Helmrelease created.")
            data = {
            'logs': 'Helm release created.\n',
            'status': 'running',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
            }
            pusher_client.trigger(str(app_uuid), 'deployment', data)
            response = requests.post(f"{sb_url}/deployments/", json=data, verify=False)
            #response = requests.post(f"{sb_url}/helm-status/", json=data)


def get_app_status(namespace, name, logger):
    api = client.CustomObjectsApi()
    try:
        app = api.get_namespaced_custom_object(
            group="dev.shapeblock.com",
            version="v1alpha1",
            name=name,
            namespace=namespace,
            plural="applications",
        )
    except ApiException as error:
        if error.status == 404:
            logger.error(f"??? Application {name} not found in namespace {namespace}.")
            return
    return app['status']

def update_app_status(namespace, name, tag, logger):
    api = client.CustomObjectsApi()
    try:
        app = api.get_namespaced_custom_object(
            group="dev.shapeblock.com",
            version="v1alpha1",
            name=name,
            namespace=namespace,
            plural="applications",
        )
    except ApiException as error:
        if error.status == 404:
            logger.error(f"??? Application {name} not found in namespace {namespace}.")
            return
    try:
        patched_body = {'status': {'lastTag' : tag }}
        response = api.patch_namespaced_custom_object_status(
            group="dev.shapeblock.com",
            version="v1alpha1",
            namespace=namespace,
            name=name,
            plural="applications",
            body=patched_body,
        )
        logger.info(f"Application {name} patched with tag {tag}.")
    except ApiException as error:
        logger.error(f"??? Unable to update status of application {name} in namespace {namespace}.")

def update_app_deployment_status(namespace, name, deployment, logger):
    api = client.CustomObjectsApi()
    try:
        app = api.get_namespaced_custom_object(
            group="dev.shapeblock.com",
            version="v1alpha1",
            name=name,
            namespace=namespace,
            plural="applications",
        )
    except ApiException as error:
        if error.status == 404:
            logger.error(f"??? Application {name} not found in namespace {namespace}.")
            return
    try:
        patched_body = {
            'status': {
                'update_app': {
                    'lastDeployment' : deployment,
                },
            }
        }
        response = api.patch_namespaced_custom_object_status(
            group="dev.shapeblock.com",
            version="v1alpha1",
            namespace=namespace,
            name=name,
            plural="applications",
            body=patched_body,
        )
        logger.info(f"Application {name} patched with deployment ID {deployment}.")
    except ApiException as error:
        logger.error(f"??? Unable to update deployment status of application {name} in namespace {namespace}.")

@kopf.on.create('nodes')
def add_node(status, name, logger, **kwargs):
    node_data = []
    node_info = {
        'name': name,
        'memory': status['capacity']['memory'],
    }
    node_data.append(node_info)
    response = requests.post(f"{sb_url}/clusters/{cluster_id}/nodes", json=node_data, verify=False)
    if response.status_code == 201:
        logger.info(f"POSTed node info for node {name}.")

@kopf.on.delete('nodes')
def remove_node(status, name, logger, **kwargs):
    node_data = []
    node_info = {
        'name': name,
        'memory': status['capacity']['memory'],
    }
    node_data.append(node_info)
    response = requests.post(f"{sb_url}/clusters/{cluster_id}/nodes-delete", json=node_data, verify=False)
    if response.status_code == 201:
        logger.info(f"DELETEd node info for node {name}.")

def get_nodes_info():
    node_data = []
    config.load_incluster_config()
    core_v1 = client.CoreV1Api()
    nodes = core_v1.list_node()
    for node in nodes.items:
        node_info = {
            'name': node.metadata.name,
            'memory': node.status.capacity['memory'],
        }
        node_data.append(node_info)
    return node_data

# Update the stack run image every 12 hours
# TODO: update the build image as well
"""
@kopf.timer('kpack.io', 'v1alpha2', 'clusterstack', interval=(3600.0 * 12))
def update_run_image_sha(name, spec, status, logger, **kwargs):
    logger.info(status['runImage'].get('latestImage'))
    # get spec run image, if having not having sha, update.
    run_image = spec['runImage']['image']
    api = client.CustomObjectsApi()
    response = requests.get('https://registry.hub.docker.com/v2/namespaces/paketobuildpacks/repositories/run/tags?page_size=1')
    if response.status_code != 200:
        logger.error('Unable to fetch run image info from registry.')
        return
    data = response.json()['results']
    new_run_image_sha = data[0]['digest']
    if run_image == 'paketobuildpacks/run:full-cnb':
        logger.info('Updating run image')
        update_run_image(api, logger, f'paketobuildpacks/run@{new_run_image_sha}')
    else:
        # if having sha, get status latest image
        # fetch latest image, if it is different, then update spec image.
        existing_run_image_sha = run_image.split('@')[1]
        if existing_run_image_sha != new_run_image_sha:
            logger.info(f'found updated image {new_run_image_sha} over existing image {existing_run_image_sha}.')
            update_run_image(api, logger, f'paketobuildpacks/run@{new_run_image_sha}')

def update_run_image(api, logger, run_image_sha):
    patch_body = {
        'spec': {
            'runImage': {
                'image': run_image_sha,
            },
        },
    }
    logger.debug(patch_body)
    response = api.patch_cluster_custom_object(
        group="kpack.io",
        version="v1alpha2",
        name='base',
        plural="clusterstacks",
        body=patch_body,
    )
    logger.info("Clusterstack patched.")
"""
