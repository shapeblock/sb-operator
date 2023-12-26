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

CHUNK_SIZE = 5000
"""
TODO:
Failure scenarios
-----------------
1. builder creation fails
2. image creation fails
3. builder update fails
4. image update fails
5. image push fails
6. build fails
7. helm deploy fails
8. helm create fails
"""

def trigger_chunked(pusher_client, app_uuid, event, data):
    chunk_size = CHUNK_SIZE
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
        logger.error(f"An application {name} is created in {namespace} without the 'shapeblock.com/app-uuid' label.")
        return
    logger.debug(f"An application is created with spec: {spec}")
    api = client.CustomObjectsApi()
    # create service account
    #TODO: handle exception if already created
    # why create a service account? A: if we're dealing with private repos.
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
    chart_values = yaml.safe_load(spec['chart']['values'])
    deployment_uuid = chart_values['universal-chart']['generic']['labels']['deployUuid']
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
            try:
                tag = spec.get('tag')
                stack = spec.get('stack')
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
            except Exception as e:
                logger.error(e)
                logger.error("Unable to create builder.")
                data = {
                    'logs': 'Unable to create builder.\n',
                    'status': 'failed',
                    'app_uuid': app_uuid,
                    'deployment_uuid': deployment_uuid,
                }
                if deployment_uuid:
                    data['deployment_uuid'] = deployment_uuid
                pusher_client.trigger(str(app_uuid), 'deployment', data)
                response = requests.post(f"{sb_url}/deployments/", json=data)
                return

            logger.info("Builder created.")
            data = {
            'logs': 'Builder created.\n',
            'status': 'running',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
            }
            if deployment_uuid:
                data['deployment_uuid'] = deployment_uuid
            pusher_client.trigger(str(app_uuid), 'deployment', data)
            response = requests.post(f"{sb_url}/deployments/", json=data)
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
            #TODO: handle exception here
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
            response = requests.post(f"{sb_url}/deployments/", json=data)
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
        if len(data['logs']) > CHUNK_SIZE:
            trigger_chunked(pusher_client, str(app_uuid), 'chunked-deployment', dict(data))
        else:
            if len(data['logs']):
                pusher_client.trigger(str(app_uuid), 'deployment', data)
        response = requests.post(f"{sb_url}/deployments/", json=data)

@kopf.on.field('kpack.io', 'v1alpha2', 'builds', field='status.conditions')
def trigger_helm_release(name, namespace, labels, spec, status, new, logger, **kwargs):
    logger.info(f"Update handler for build with status: {status}")
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return
    app_name = labels['image.kpack.io/image']
    app_status = get_app_status(namespace, app_name, logger)
    is_new_app = True
    if 'update_app' in app_status.keys():
        deployment_uuid = app_status['update_app'].get('lastDeployment')
        is_new_app = False
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
        response = requests.post(f"{sb_url}/deployments/", json=data)
        app_object = get_app_object(app_name, namespace, logger)
        if is_new_app:
            create_helmrelease(name=app_name, app_uuid=app_uuid, app_spec=app_object['spec'], namespace=namespace, tag=tag, logger=logger)
        else:
            update_helmrelease(name=app_name, app_uuid=app_uuid, app_spec=app_object['spec'], namespace=namespace, tag=tag, logger=logger)
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
        response = requests.post(f"{sb_url}/deployments/", json=data)

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
    chart_values = yaml.safe_load(spec['chart']['values'])
    deployment_uuid = chart_values['universal-chart']['generic']['labels']['deployUuid']
    deployment_type = labels.get('shapeblock.com/deployment-type')
    if deployment_type == 'config':
        tag = get_last_tag(status)
        # update app status with last deployment
        update_app_deployment_status(namespace, name, deployment_uuid, logger)
        # Trigger a helm release if it's only a config change
        update_helmrelease(name, app_uuid, spec, namespace, tag, logger)
        return {'lastDeployment': deployment_uuid}
    api = client.CustomObjectsApi()
    git_info = spec.get('git')
    ref = git_info.get('ref')
    chart_info = spec.get('chart')
    build_envs = chart_info.get('build')
    # TODO: Create image and build if it doesn't exist already
    # If build config change, add a build env var. It is harmless and triggers a new build.
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
    # TODO: remove this.
    # build_ts = {
    #             "name": "SB_TS",
    #             "value": str(datetime.datetime.now()),
    # }
    # patch_body['spec']['build']['env'].append(build_ts)
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
    response = requests.post(f"{sb_url}/deployments/", json=data)
    return {'lastDeployment': deployment_uuid}


@kopf.on.update('helm.toolkit.fluxcd.io', 'v2beta1', 'helmreleases', field='status')
def helm_release_status(name, namespace, spec, diff, labels, status, logger, **kwargs):
    logger.info('--- helm release status ---')
    deployment_uuid = spec['values']['universal-chart']['generic']['labels']['deployUuid']
    logger.info(f'deployment UUID: {deployment_uuid}')
    logger.info(diff)
    logger.info(status)
    history = status.get('history')
    conditions = status.get('conditions')
    if history and conditions:
        if history[0]['status'] == 'deployed':
            status = 'success'
        if history[0]['status'] == 'failed':
            status = 'failed'
        if status:
            app_uuid = labels.get('shapeblock.com/app-uuid')
            data = {
                'logs': conditions[-1]['message'],
                'status': status,
                'app_uuid': app_uuid,
                'deployment_uuid': deployment_uuid,
            }
            pusher_client.trigger(str(app_uuid), 'deployment', data)
            response = requests.post(f"{sb_url}/deployments/", json=data)




@kopf.on.delete('applications')
def delete_app(spec, name, namespace, labels, logger, **kwargs):
    logger.debug(f"An application is deleted with spec: {spec}")
    app_uuid = labels.get('shapeblock.com/app-uuid')
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
    job_label = f"appUuid={app_uuid}"
    batch_v1 = client.BatchV1Api()
    core_v1 = client.CoreV1Api()
    jobs = batch_v1.list_namespaced_job(namespace=namespace, label_selector=job_label)
    for job in jobs.items:
        resp = batch_v1.delete_namespaced_job(namespace=namespace, body=client.V1DeleteOptions(), name=job.metadata.name)
        pod_label = f"job-name={job.metadata.name}"
        resp = core_v1.delete_namespaced_pod(namespace=namespace, body=client.V1DeleteOptions(), label_selector=pod_label)
        logger.info(f'Deleting Job {job.metadata.name}')
    logger.info("jobs deleted.")
    # Delete ingress tls secret
    # Delete volumes if any
    core_v1 = client.CoreV1Api()
    label = f"app.kubernetes.io/instance={name}"
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
    logger.info("send notification to SB server.")


def get_app_object(name, namespace, logger):
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
    return app

# TODO: daemon to update kpack base images
# TODO: daemon to send status to SB every x hrs

def create_helmrelease(name, app_uuid, app_spec, namespace, tag, logger):
    api = client.CustomObjectsApi()
    _, image_tag = tag.split(':')
    logger.debug(f"App spec: {app_spec}.")
    chart_info = app_spec.get('chart')
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
    helm_data = yaml.safe_load(text)
    helm_data['spec']['values'] = yaml.safe_load(chart_values)
    helm_data['spec']['values']['universal-chart']['defaultImageTag'] = image_tag
    deployment_uuid = helm_data['spec']['values']['universal-chart']['generic']['labels']['deployUuid']
    response = api.create_namespaced_custom_object(
        group="helm.toolkit.fluxcd.io",
        version="v2beta1",
        namespace=namespace,
        plural="helmreleases",
        body=helm_data,
    )
    logger.info("Helmrelease created.")
    data = {
    'logs': 'Helm release created.\n',
    'status': 'running',
    'app_uuid': app_uuid,
    'deployment_uuid': deployment_uuid,
    }
    pusher_client.trigger(str(app_uuid), 'deployment', data)
    response = requests.post(f"{sb_url}/deployments/", json=data)


def update_helmrelease(name, app_uuid, app_spec, namespace, tag, logger):
    api = client.CustomObjectsApi()
    logger.debug(f"App spec: {app_spec}.")
    _, image_tag = tag.split(':')
    logger.info("Helm Release exists, patching ...")
    chart_info = app_spec.get('chart')
    chart_values = chart_info.get('values')
    # TODO: patch deployment UUID
    helm_data = {
        "spec": {
            "chart": {
                # Add chart version from spec
                "version": chart_info.get('version'),
            },
            "values": yaml.safe_load(chart_values),
        }
    }
    helm_data['spec']['values']['universal-chart']['defaultImageTag'] = image_tag
    deployment_uuid = helm_data['spec']['values']['universal-chart']['generic']['labels']['deployUuid']
    response = api.patch_namespaced_custom_object(
        group="helm.toolkit.fluxcd.io",
        version="v2beta1",
        namespace=namespace,
        name=name,
        plural="helmreleases",
        body=helm_data,
    )
    logger.info(f"Patched custom resource. Response: {response}")
    #TODO: check response
    logger.info("Helmrelease patched.")
    data = {
        'logs': 'Patched Helm Release.\n',
        'status': 'running',
        'app_uuid': app_uuid,
        'deployment_uuid': deployment_uuid,
    }
    pusher_client.trigger(str(app_uuid), 'deployment', data)
    response = requests.post(f"{sb_url}/deployments/", json=data)


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
