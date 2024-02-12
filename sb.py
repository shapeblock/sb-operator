import os
import time
import datetime
from typing import Dict
import requests
import kopf
from kubernetes import client, config
import yaml
from kubernetes.client.rest import ApiException
from pprint import pformat


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
    tag = spec.get('tag')
    ref = git_info.get('ref')
    sub_path = git_info.get('subPath')
    stack = spec.get('stack')
    chart_info = spec.get('chart')

    # create builder
    if not builder_exists(name, namespace):
        try:
            create_builder(name, namespace, tag, stack, app_uuid)
            logger.info("Builder created.")
            data = {
            'logs': 'Builder created.\n',
            'status': 'running',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
            }
            response = requests.post(f"{sb_url}/deployments/", json=data)
        except Exception as e:
            logger.error(e)
            logger.error("Unable to create builder.")
            data = {
                'logs': 'Unable to create builder.\n',
                'status': 'failed',
                'app_uuid': app_uuid,
                'deployment_uuid': deployment_uuid,
            }
            response = requests.post(f"{sb_url}/deployments/", json=data)
            return

    # create image
    if not image_exists(name, namespace):
        try:
            create_image(name, namespace, app_uuid, tag, repo, ref, sub_path, chart_info)
            logger.info("Image created.")
            data = {
            'logs': 'Image created.\n',
            'app_uuid': app_uuid,
            'status': 'running',
            'deployment_uuid': deployment_uuid,
            }
            response = requests.post(f"{sb_url}/deployments/", json=data)
        except Exception as e:
            logger.error(e)
            logger.error("Unable to create image.")
            data = {
                'logs': 'Unable to create image.\n',
                'status': 'failed',
                'app_uuid': app_uuid,
                'deployment_uuid': deployment_uuid,
            }
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

    if status.get('type') == 'Succeeded' and status.get('status') == 'True':
        logger.info('BUILD step failed')
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
    steps_completed = status.get('stepsCompleted')
    pod_name = status['podName']
    rebase = steps_completed and 'rebase' in steps_completed
    if rebase:
        app_object = get_app_object(app_name, namespace, logger)
        logger.info(f"Updating helm release for {app_name} for a rebase.")
        tag = spec.get('tags')[1]
        update_helmrelease(name=app_name, app_uuid=app_uuid, app_spec=app_object['spec'], namespace=namespace, tag=tag, logger=logger)
        return
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
        response = requests.post(f"{sb_url}/deployments/", json=data)
        app_object = get_app_object(app_name, namespace, logger)
        # Sometimes, last deployment might have failed and helm object might not have created
        if not helmrelease_exists(name, namespace):
                is_new_app = True

        if is_new_app:
            create_helmrelease(name=app_name, app_uuid=app_uuid, app_spec=app_object['spec'], namespace=namespace, tag=tag, logger=logger)
        else:
            update_helmrelease(name=app_name, app_uuid=app_uuid, app_spec=app_object['spec'], namespace=namespace, tag=tag, logger=logger)
        # Update the latest image tag in app status
        update_app_status(namespace, app_name, tag, logger)
    if status.get('type') == 'Succeeded' and status.get('status') == 'False':
        # end logs of failed stage

        data = {
            'logs': 'Build stage failed.\n',
            'status': 'failed',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
        }

        logger.info(steps_completed)
        if steps_completed:
            previous_step = steps_completed[-1]
            if previous_step == 'prepare':
                failed_step = 'analyze'
            if previous_step == 'analyze':
                failed_step = 'detect'
            if previous_step == 'detect':
                failed_step = 'restore'
            if previous_step == 'restore':
                failed_step = 'build'
            if previous_step == 'build':
                failed_step = 'export'
            if previous_step == 'export':
                failed_step = 'completion'

            logger.info(previous_step)
            logger.info(failed_step)
            core_v1 = client.CoreV1Api()
            failed_step_logs = core_v1.read_namespaced_pod_log(namespace=namespace, name=pod_name, container=failed_step)
            logger.info(failed_step_logs)
            data['logs'] += failed_step_logs
        response = requests.post(f"{sb_url}/deployments/", json=data)

def get_last_tag(status: Dict):
    """
    Get last deployed tag from application status.
    """
    return status.get('lastTag')

@kopf.on.update('applications')
def update_app(spec, name, namespace, logger, labels, status, **kwargs):
    logger.debug(f"An application is updated with spec: {spec}")
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return
    chart_values = yaml.safe_load(spec['chart']['values'])
    deployment_uuid = chart_values['universal-chart']['generic']['labels']['deployUuid']
    deployment_type = labels.get('shapeblock.com/deployment-type')
    tag = get_last_tag(status)

    if deployment_type == 'config':
        # update app with last deployment id
        update_app_deployment_id(namespace, name, deployment_uuid, logger)
        # Trigger a helm release if it's only a config change
        update_helmrelease(name, app_uuid, spec, namespace, tag, logger)
        return {'lastDeployment': deployment_uuid}
    api = client.CustomObjectsApi()
    git_info = spec.get('git')
    repo = git_info.get('repo')
    ref = git_info.get('ref')
    chart_info = spec.get('chart')
    sub_path = git_info.get('subPath')
    stack = spec.get('stack')
    build_envs = chart_info.get('build')
    # TODO: Create build if it doesn't exist already

    if not image_exists(name, namespace):
        # app status last tag will be empty if image doesn't exist
        tag = spec.get('tag')
        try:
            create_image(name, namespace, app_uuid, tag, repo, ref, sub_path, chart_info)
            logger.info("Image created.")
            data = {
            'logs': 'Image created.\n',
            'app_uuid': app_uuid,
            'status': 'running',
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
            }
            response = requests.post(f"{sb_url}/deployments/", json=data)
        except Exception as e:
            logger.error(e)
            logger.error("Unable to create image.")
            data = {
                'logs': 'Unable to create image.\n',
                'status': 'failed',
                'app_uuid': app_uuid,
                'deployment_uuid': deployment_uuid,
            }
            response = requests.post(f"{sb_url}/deployments/", json=data)
    else:
        logger.info(f"Tag status: {tag}")
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

        # If build config change, add a build env var. It is harmless and triggers a new build.
        tag = get_last_tag(status)
        if not tag:
            build_ts = {
                        "name": "SB_TS",
                        "value": str(datetime.datetime.now()),
            }
            patch_body['spec']['build']['env'].append(build_ts)

        #TODO: don't patch image, trigger a helm release instead if ref before patching is same as new ref
        logger.debug(patch_body)
        try:
            response = api.patch_namespaced_custom_object(
                group="kpack.io",
                version="v1alpha2",
                namespace=namespace,
                name=name,
                plural="images",
                body=patch_body,
            )
            logger.info("Image patched.")
        except ApiException as error:
            logger.info(f"Unable to patch image for {name}: {error}.")

    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return
    data = {
        'logs': 'Patched image.\n',
        'status': 'running',
        'app_uuid': app_uuid,
        'deployment_uuid': deployment_uuid,
    }
    response = requests.post(f"{sb_url}/deployments/", json=data)
    return {'lastDeployment': deployment_uuid}


@kopf.on.update('helm.toolkit.fluxcd.io', 'helmreleases', field='status')
def helm_release_status(name, namespace, spec, diff, labels, status, logger, **kwargs):
    logger.info('--- helm release status ---')
    service_uuid = labels.get('shapeblock.com/service-uuid')
    if service_uuid:
        return
    deployment_uuid = spec['values']['universal-chart']['generic']['labels']['deployUuid']
    logger.info(f'deployment UUID: {deployment_uuid}')
    app_status = get_app_status(namespace, name, logger)
    if 'update_app' in app_status.keys():
        app_deployment_uuid = app_status['update_app'].get('lastDeployment')
    else:
        app_deployment_uuid = app_status['create_app'].get('lastDeployment')
    history = status.get('history')
    conditions = status.get('conditions')
    if not history:
        return
    if (app_deployment_uuid == deployment_uuid) and (app_status.get('lastDeployedVersion') == history[0]['version']):
        return
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
            update_app_deployment_status(namespace, name, history[0]['version'], logger)
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
            version="v2beta2",
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

@kopf.on.delete('helmreleases')
def delete_helmrelease(name, namespace, labels, logger, **kwargs):
    service_uuid = labels.get('shapeblock.com/service-uuid')
    if not service_uuid:
        return
    core_v1 = client.CoreV1Api()
    label = f"app.kubernetes.io/instance={name}"
    pvcs = core_v1.list_namespaced_persistent_volume_claim(namespace=namespace, label_selector=label)
    for pvc in pvcs.items:
        resp = core_v1.delete_namespaced_persistent_volume_claim(namespace=namespace, body=client.V1DeleteOptions(), name=pvc.metadata.name)
        logger.info(f'Deleting PVC {pvc.metadata.name}')
    logger.info("volumes deleted.")


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
    try:
        response = api.create_namespaced_custom_object(
            group="helm.toolkit.fluxcd.io",
            version="v2beta2",
            namespace=namespace,
            plural="helmreleases",
            body=helm_data,
        )
    except ApiException as error:
        if error.status == 409:
            logger.error(f"Helm release already exists for {name}.")
            update_helmrelease(name, app_uuid, app_spec, namespace, tag, logger)
            return

    logger.info("Helmrelease created.")
    data = {
    'logs': 'Helm release created.\n',
    'status': 'running',
    'app_uuid': app_uuid,
    'deployment_uuid': deployment_uuid,
    }
    response = requests.post(f"{sb_url}/deployments/", json=data)


def update_helmrelease(name, app_uuid, app_spec, namespace, tag, logger):
    api = client.CustomObjectsApi()
    logger.debug(f"App spec: {app_spec}.")
    _, image_tag = tag.split(':')
    logger.info("Helm Release exists, patching ...")
    chart_info = app_spec.get('chart')
    chart_values = chart_info.get('values')
    chart_name = chart_info.get('name')
    chart_repo = chart_info.get('repo')
    chart_version = chart_info.get('version')

    # get current resource for resourceVersion
    current_resource = api.get_namespaced_custom_object(
        group="helm.toolkit.fluxcd.io",
        version="v2beta2",
        namespace=namespace,
        name=name,
        plural="helmreleases",
    )

    resource_version = current_resource['metadata']['resourceVersion']

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
    helm_data['metadata']['resourceVersion'] =  resource_version
    logger.info(pformat(helm_data))
    response = api.replace_namespaced_custom_object(
        group="helm.toolkit.fluxcd.io",
        version="v2beta2",
        namespace=namespace,
        name=name,
        plural="helmreleases",
        body=helm_data,
    )
    #TODO: check response
    logger.info("Helmrelease updated.")
    data = {
        'logs': 'Updated Helm Release.\n',
        'status': 'running',
        'app_uuid': app_uuid,
        'deployment_uuid': deployment_uuid,
    }
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

def update_app_deployment_id(namespace, name, deployment, logger):
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
        logger.error(f"??? Unable to update deployment ID of application {name} in namespace {namespace}.")

def update_app_deployment_status(namespace, name, deployed_version, logger):
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
                'lastDeployedVersion' : deployed_version,
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
        logger.info(f"Application {name} patched with deployment status {deployed_version}.")
    except ApiException as error:
        logger.error(f"??? Unable to update deployment status of application {name} in namespace {namespace}.")


def builder_exists(name, namespace):
    api = client.CustomObjectsApi()
    try:
        resource = api.get_namespaced_custom_object(
            group="kpack.io",
            version="v1alpha2",
            name=name,
            namespace=namespace,
            plural="builders",
        )
        return True
    except ApiException as error:
        if error.status == 404:
            return False


def create_builder(name, namespace, tag, stack, app_uuid):
    path = os.path.join(os.path.dirname(__file__), f'builder-{stack}.yaml')
    tmpl = open(path, 'rt').read()
    text = tmpl.format(name=name, tag=tag, service_account=name, app_uuid=app_uuid)
    data = yaml.safe_load(text)

    api = client.CustomObjectsApi()
    response = api.create_namespaced_custom_object(
        group="kpack.io",
        version="v1alpha2",
        namespace=namespace,
        plural="builders",
        body=data,
    )


def image_exists(name, namespace):
    api = client.CustomObjectsApi()
    try:
        resource = api.get_namespaced_custom_object(
            group="kpack.io",
            version="v1alpha2",
            name=name,
            namespace=namespace,
            plural="images",
        )
        return True
    except ApiException as error:
        if error.status == 404:
            return False

def create_image(name, namespace, app_uuid, tag, repo, ref, sub_path, chart_info):
    service_account = name
    builder = name
    if sub_path:
        path = os.path.join(os.path.dirname(__file__), 'image_subpath.yaml')
        tmpl = open(path, 'rt').read()
        text = tmpl.format(name=name, tag=tag, service_account=service_account, repo=repo, ref=ref, builder_name=builder, app_uuid=app_uuid, sub_path=sub_path)
    else:
        path = os.path.join(os.path.dirname(__file__), 'image.yaml')
        tmpl = open(path, 'rt').read()
        text = tmpl.format(name=name, tag=tag, service_account=service_account, repo=repo, ref=ref, builder_name=builder, app_uuid=app_uuid)
    data = yaml.safe_load(text)
    build_envs = chart_info.get('build')
    if build_envs:
        data['spec']['build'] = {'env' : build_envs}

    api = client.CustomObjectsApi()
    response = api.create_namespaced_custom_object(
        group="kpack.io",
        version="v1alpha2",
        namespace=namespace,
        plural="images",
        body=data,
    )

def helmrelease_exists(name, namespace):
    api = client.CustomObjectsApi()
    try:
        resource = api.get_namespaced_custom_object(
            group="helm.toolkit.fluxcd.io",
            version="v2beta2",
            name=name,
            namespace=namespace,
            plural="helmreleases",
        )
        return True
    except ApiException as error:
        if error.status == 404:
            return False
