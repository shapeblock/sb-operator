import os
import time
import datetime
from enum import Enum
from dataclasses import dataclass
from typing import Dict, Optional
import requests
import kopf
from kubernetes import client, config
import yaml
from kubernetes.client.rest import ApiException
from pprint import pformat

class BuildStatus(Enum):
    PENDING = "pending"
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

class DeployStatus(Enum):
    PENDING = "pending"
    DEPLOYING = "deploying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

@dataclass
class AppStatus:
    build_status: BuildStatus
    deploy_status: DeployStatus
    last_error: Optional[str] = None
    current_tag: Optional[str] = None

class KubernetesClientManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, 'initialized'):
            self.core_v1 = None
            self.custom_objects = None
            self.rbac_v1 = None
            self.initialize_clients()
            self.initialized = True

    def initialize_clients(self):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

        self.core_v1 = client.CoreV1Api()
        self.custom_objects = client.CustomObjectsApi()
        self.rbac_v1 = client.RbacAuthorizationV1Api()

    def refresh_clients(self):
        self.initialize_clients()

    def execute_with_retry(self, operation, max_retries=3):
        for attempt in range(max_retries):
            try:
                return operation()
            except ApiException as e:
                if e.status == 401 and attempt < max_retries - 1:
                    kopf.info(f"Authentication failed, refreshing clients (attempt {attempt + 1})")
                    self.refresh_clients()
                else:
                    raise

# Initialize the global client manager
k8s_client = KubernetesClientManager()


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
    if sb_url.startswith('http://'):
        return sb_url
    return f'https://{sb_url}'

sb_url = get_sb_url(os.getenv('SB_URL'))

@kopf.on.probe(id='version')
def get_version(**kwargs):
    v1 = client.VersionApi()
    version = v1.get_code()
    return version.to_dict()

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
    registry_creds = core_v1.read_namespaced_secret(namespace='shapeblock', name='registry-creds')
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
        raise kopf.PermanentError("Missing app_uuid label")

    deployment_uuid = get_deployment_uuid_from_spec(spec)
    app_status = AppStatus(
        build_status=BuildStatus.PENDING,
        deploy_status=DeployStatus.PENDING
    )

    try:
        # Create service account with retry
        try:
            service_account = k8s_client.execute_with_retry(
                lambda: create_service_account(name, namespace, logger)
            )
            logger.info("Service account created successfully")
        except ApiException as e:
            if e.status != 409:  # Ignore if already exists
                raise

        # Handle git credentials if needed
        git_info = spec.get('git')
        if git_info and git_info.get('repo', '').startswith('git@'):
            try:
                attach_ssh_secret(name, namespace, service_account, logger)
            except Exception as e:
                logger.error(f"Failed to attach SSH secret: {str(e)}")
                raise

        # Create builder with retry
        if not builder_exists(name, namespace):
            try:
                create_builder_with_retry(name, namespace, spec, logger)
                logger.info("Builder created successfully")
                update_status(
                    app_uuid=app_uuid,
                    status="running",
                    logs="Builder created successfully",
                    deployment_uuid=deployment_uuid
                )
            except Exception as e:
                logger.error(f"Builder creation failed: {str(e)}")
                update_status(
                    app_uuid=app_uuid,
                    status="failed",
                    logs=f"Builder creation failed: {str(e)}",
                    deployment_uuid=deployment_uuid
                )
                raise kopf.TemporaryError(f"Builder creation failed: {str(e)}", delay=300)

        # Create image with retry
        if not image_exists(name, namespace):
            try:
                create_image_with_retry(
                    name=name,
                    namespace=namespace,
                    app_uuid=app_uuid,
                    spec=spec,
                    logger=logger
                )
                logger.info("Image created successfully")
                update_status(
                    app_uuid=app_uuid,
                    status="running",
                    logs="Image created successfully",
                    deployment_uuid=deployment_uuid
                )
            except Exception as e:
                logger.error(f"Image creation failed: {str(e)}")
                update_status(
                    app_uuid=app_uuid,
                    status="failed",
                    logs=f"Image creation failed: {str(e)}",
                    deployment_uuid=deployment_uuid
                )
                raise kopf.TemporaryError(f"Image creation failed: {str(e)}", delay=300)

        return {'lastDeployment': deployment_uuid}

    except Exception as e:
        logger.error(f"Application creation failed: {str(e)}")
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Application creation failed: {str(e)}",
            deployment_uuid=deployment_uuid
        )
        raise kopf.TemporaryError(str(e), delay=300)

def create_builder_with_retry(name, namespace, spec, logger, max_retries=3):
    """Create builder with retries for transient failures"""
    def create_builder_operation():
        return create_builder(
            name=name,
            namespace=namespace,
            tag=spec.get('tag'),
            stack=spec.get('stack'),
            app_uuid=spec.get('app_uuid')
        )

    return k8s_client.execute_with_retry(create_builder_operation)

def create_image_with_retry(name, namespace, app_uuid, spec, logger, max_retries=3):
    """Create image with retries for transient failures"""
    git_info = spec.get('git', {})
    def create_image_operation():
        return create_image(
            name=name,
            namespace=namespace,
            app_uuid=app_uuid,
            tag=spec.get('tag'),
            repo=git_info.get('repo'),
            ref=git_info.get('ref'),
            sub_path=git_info.get('subPath'),
            chart_info=spec.get('chart')
        )

    return k8s_client.execute_with_retry(create_image_operation)

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
        data['pod'] = status['podName']
        response = requests.post(f"{sb_url}/deployments/", json=data)

@kopf.on.field('kpack.io', 'v1alpha2', 'builds', field='status.conditions')
def trigger_helm_release(name, namespace, labels, spec, status, new, logger, **kwargs):
    """Handle build status changes with improved error handling and status tracking"""
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        return

    try:
        app_name = labels['image.kpack.io/image']
        app_status = get_app_status(namespace, app_name, logger)

        if 'update_app' in app_status:
            deployment_uuid = app_status['update_app'].get('lastDeployment')
        else:
            deployment_uuid = app_status['create_app'].get('lastDeployment')

        # Check for rebase operation first
        steps_completed = status.get('stepsCompleted', [])
        if steps_completed and 'rebase' in steps_completed:
            logger.info(f"Handling rebase operation for {app_name}")
            app_object = get_app_object(app_name, namespace, logger)
            tag = spec.get('tags', [])[1] if len(spec.get('tags', [])) > 1 else None
            if tag:
                update_helmrelease(
                    name=app_name,
                    app_uuid=app_uuid,
                    app_spec=app_object['spec'],
                    namespace=namespace,
                    tag=tag,
                    logger=logger
                )
            return

        # Process build status
        conditions = new[0]
        current_condition = conditions.get('type')
        current_status = conditions.get('status')
        logger.info(f"Current condition: {current_condition}, Current status: {current_status}")
        logger.info(f"Status: {status}")

        # Handle different build phases
        if current_condition == 'Succeeded':
            if current_status == 'True':
                handle_successful_build(
                    name=app_name,
                    namespace=namespace,
                    app_uuid=app_uuid,
                    spec=spec,
                    status=status,
                    deployment_uuid=deployment_uuid,
                    logger=logger
                )
            elif current_status == 'False':
                # Get the failed step logs
                failed_step = None
                if steps_completed:
                    previous_step = steps_completed[-1]
                    step_mapping = {
                        'prepare': 'analyze',
                        'analyze': 'detect',
                        'detect': 'restore',
                        'restore': 'build',
                        'build': 'export',
                        'export': 'completion'
                    }
                    failed_step = step_mapping.get(previous_step)

                handle_failed_build(
                    name=app_name,
                    namespace=namespace,
                    app_uuid=app_uuid,
                    spec=spec,
                    status=status,
                    deployment_uuid=deployment_uuid,
                    failed_step=failed_step,
                    logger=logger
                )
        else:
            # Handle build in progress
            handle_build_progress(
                name=app_name,
                namespace=namespace,
                app_uuid=app_uuid,
                status=status,
                deployment_uuid=deployment_uuid,
                logger=logger
            )

    except Exception as e:
        logger.error(f"Error handling build status: {str(e)}")
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Build status handling failed: {str(e)}",
            deployment_uuid=deployment_uuid
        )

def handle_build_progress(name: str, namespace: str, app_uuid: str,
                         status: dict, deployment_uuid: str, logger):
    """Handle build in progress status"""
    try:
        # Get current step from status
        steps_completed = status.get('stepsCompleted', [])
        current_step = steps_completed[-1] if steps_completed else "Initializing"

        # Try to get logs from the last completed step
        build_logs = ""
        if status.get('podName'):
            try:
                # Only try to get logs if pod is running
                pod = k8s_client.execute_with_retry(
                    lambda: k8s_client.core_v1.read_namespaced_pod(
                        namespace=namespace,
                        name=status['podName']
                    )
                )

                if pod.status.phase == 'Running':
                    container = steps_completed[-1] if steps_completed else None
                    if container:
                        build_logs = k8s_client.execute_with_retry(
                            lambda: k8s_client.core_v1.read_namespaced_pod_log(
                                namespace=namespace,
                                name=status['podName'],
                                container=container
                            )
                        )
            except ApiException as e:
                if e.status != 400:  # Ignore 400 errors during initialization
                    logger.warning(f"Failed to get build logs: {str(e)}")

        # Update status with progress
        update_status(
            app_uuid=app_uuid,
            status="running",
            logs=f"Build in progress: {current_step}\n\n{build_logs}",
            deployment_uuid=deployment_uuid
        )

    except Exception as e:
        logger.error(f"Failed to handle build progress: {str(e)}")

def handle_successful_build(name, namespace, app_uuid, spec, status, deployment_uuid, is_new_app, logger):
    try:
        tag = spec.get('tags')[1]
        logger.info(f"Build successful. New image tag: {tag}")

        update_status(
            app_uuid=app_uuid,
            status="running",
            logs="Build completed successfully, initiating deployment",
            deployment_uuid=deployment_uuid
        )

        app_object = get_app_object(name, namespace, logger)
        if not helmrelease_exists(name, namespace):
            create_helmrelease(
                name=name,
                app_uuid=app_uuid,
                app_spec=app_object['spec'],
                namespace=namespace,
                tag=tag,
                logger=logger
            )
        else:
            update_helmrelease(
                name=name,
                app_uuid=app_uuid,
                app_spec=app_object['spec'],
                namespace=namespace,
                tag=tag,
                logger=logger
            )

        update_app_status(namespace, name, tag, logger)

    except Exception as e:
        logger.error(f"Post-build processing failed: {str(e)}")
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Post-build processing failed: {str(e)}",
            deployment_uuid=deployment_uuid
        )
        raise

def get_last_tag(status: Dict):
    """
    Get last deployed tag from application status.
    """
    return status.get('lastTag')

@kopf.on.update('applications')
def update_app(spec, name, namespace, logger, labels, status, **kwargs):
    """Handle application updates with improved error handling and state management"""
    logger.debug(f"An application is updated with spec: {spec}")

    # Validate required labels
    app_uuid = labels.get('shapeblock.com/app-uuid')
    if not app_uuid:
        logger.error(f"Application {name} updated without app_uuid label")
        return

    # Parse chart values
    if isinstance(spec['chart']['values'], str):
        chart_values = yaml.safe_load(spec['chart']['values'])
    else:
        chart_values = spec['chart']['values']

    deployment_uuid = chart_values['universal-chart']['generic']['labels']['deployUuid']
    deployment_type = labels.get('shapeblock.com/deployment-type')
    tag = get_last_tag(status)

    try:
        # Handle config-only updates
        if deployment_type == 'config':
            logger.info("Processing configuration-only update")
            return handle_config_update(
                namespace=namespace,
                name=name,
                app_uuid=app_uuid,
                deployment_uuid=deployment_uuid,
                spec=spec,
                tag=tag,
                logger=logger
            )

        # Handle code/build updates
        return handle_code_update(
            namespace=namespace,
            name=name,
            app_uuid=app_uuid,
            deployment_uuid=deployment_uuid,
            spec=spec,
            tag=tag,
            logger=logger
        )

    except Exception as e:
        logger.error(f"Failed to process application update: {str(e)}")
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Update failed: {str(e)}",
            deployment_uuid=deployment_uuid
        )
        raise kopf.TemporaryError(f"Update failed: {str(e)}", delay=300)

def handle_config_update(namespace: str, name: str, app_uuid: str, deployment_uuid: str,
                        spec: dict, tag: str, logger) -> dict:
    """Handle configuration-only updates"""
    try:
        # Update app with last deployment id
        update_app_deployment_id(namespace, name, deployment_uuid, logger)

        # Update helm release
        update_helmrelease(
            name=name,
            app_uuid=app_uuid,
            app_spec=spec,
            namespace=namespace,
            tag=tag,
            logger=logger
        )

        update_status(
            app_uuid=app_uuid,
            status="running",
            logs="Configuration update initiated",
            deployment_uuid=deployment_uuid
        )

        return {'lastDeployment': deployment_uuid}

    except Exception as e:
        logger.error(f"Configuration update failed: {str(e)}")
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Configuration update failed: {str(e)}",
            deployment_uuid=deployment_uuid
        )
        raise

def handle_code_update(namespace: str, name: str, app_uuid: str, deployment_uuid: str,
                      spec: dict, tag: str, logger) -> dict:
    """Handle code/build updates"""
    try:
        git_info = spec.get('git', {})
        chart_info = spec.get('chart', {})

        # Check if image exists
        if not image_exists(name, namespace):
            return handle_new_image_creation(
                namespace=namespace,
                name=name,
                app_uuid=app_uuid,
                deployment_uuid=deployment_uuid,
                spec=spec,
                logger=logger
            )

        # Handle existing image update
        return handle_image_update(
            namespace=namespace,
            name=name,
            app_uuid=app_uuid,
            deployment_uuid=deployment_uuid,
            spec=spec,
            current_tag=tag,
            logger=logger
        )

    except Exception as e:
        logger.error(f"Code update failed: {str(e)}")
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Code update failed: {str(e)}",
            deployment_uuid=deployment_uuid
        )
        raise

def handle_new_image_creation(namespace: str, name: str, app_uuid: str,
                            deployment_uuid: str, spec: dict, logger) -> dict:
    """Handle creation of new image when it doesn't exist"""
    try:
        tag = spec.get('tag')
        git_info = spec.get('git', {})

        create_image(
            name=name,
            namespace=namespace,
            app_uuid=app_uuid,
            tag=tag,
            repo=git_info.get('repo'),
            ref=git_info.get('ref'),
            sub_path=git_info.get('subPath'),
            chart_info=spec.get('chart')
        )

        logger.info("Image created successfully")
        update_status(
            app_uuid=app_uuid,
            status="running",
            logs="Image created and build initiated",
            deployment_uuid=deployment_uuid
        )

        return {'lastDeployment': deployment_uuid}

    except Exception as e:
        logger.error(f"Failed to create new image: {str(e)}")
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Failed to create image: {str(e)}",
            deployment_uuid=deployment_uuid
        )
        raise

def handle_image_update(namespace: str, name: str, app_uuid: str, deployment_uuid: str,
                       spec: dict, current_tag: str, logger) -> dict:
    """Handle updates to existing image"""
    try:
        git_info = spec.get('git', {})
        chart_info = spec.get('chart', {})
        ref = git_info.get('ref')
        build_envs = chart_info.get('build')

        # Get current image configuration
        image = k8s_client.execute_with_retry(
            lambda: k8s_client.custom_objects.get_namespaced_custom_object(
                group="kpack.io",
                version="v1alpha2",
                name=name,
                namespace=namespace,
                plural="images",
            )
        )

        current_ref = image['spec']['source']['git']['revision']

        # If no code change and has current tag, just update helm release
        if current_ref == ref and current_tag:
            logger.info("No code changes detected, updating helm release")
            if helmrelease_exists(name, namespace):
                update_helmrelease(
                    name=name,
                    app_uuid=app_uuid,
                    app_spec=spec,
                    namespace=namespace,
                    tag=current_tag,
                    logger=logger
                )
            else:
                create_helmrelease(
                    name=name,
                    app_uuid=app_uuid,
                    app_spec=spec,
                    namespace=namespace,
                    tag=current_tag,
                    logger=logger
                )

            update_status(
                app_uuid=app_uuid,
                status="running",
                logs="No code change.\nUpdating helm release.",
                deployment_uuid=deployment_uuid
            )

            return {'lastDeployment': deployment_uuid}

        # Prepare image update
        patch_body = {
            "spec": {
                "source": {
                    "git": {
                        "revision": ref,
                    }
                },
                "build": {
                    "env": build_envs or []
                }
            }
        }

        # Add build timestamp to trigger new build
        if current_tag:
            build_ts = {
                "name": "SB_TS",
                "value": str(datetime.datetime.now()),
            }
            patch_body['spec']['build']['env'].append(build_ts)

        # Update image
        logger.debug(f"Updating image with: {patch_body}")
        k8s_client.execute_with_retry(
            lambda: k8s_client.custom_objects.patch_namespaced_custom_object(
                group="kpack.io",
                version="v1alpha2",
                namespace=namespace,
                name=name,
                plural="images",
                body=patch_body,
            )
        )

        logger.info("Image updated successfully")
        update_status(
            app_uuid=app_uuid,
            status="running",
            logs="Image updated, new build initiated",
            deployment_uuid=deployment_uuid
        )

        return {'lastDeployment': deployment_uuid}

    except Exception as e:
        logger.error(f"Failed to update image: {str(e)}")
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Failed to update image: {str(e)}",
            deployment_uuid=deployment_uuid
        )
        raise

@kopf.on.update('helm.toolkit.fluxcd.io', 'helmreleases', field='status')
def helm_release_status(name, namespace, spec, diff, labels, status, logger, **kwargs):
    """Handle HelmRelease status updates with improved error handling and status management"""
    logger.info('Processing helm release status update')

    try:
        # Handle service deployments
        service_uuid = labels.get('shapeblock.com/service-uuid')
        if service_uuid:
            return handle_service_deployment_status(
                name=name,
                namespace=namespace,
                service_uuid=service_uuid,
                status=status,
                logger=logger
            )

        # Handle application deployments
        deployment_uuid = get_deployment_uuid_from_helm_values(spec)
        if not deployment_uuid:
            logger.warning(f"No deployment UUID found in HelmRelease {name}")
            return

        app_status = get_app_status(namespace, name, logger)
        app_deployment_uuid = get_deployment_uuid_from_app_status(app_status)

        # Skip if this is not the latest deployment or version already processed
        if not should_process_helm_status(
            deployment_uuid=deployment_uuid,
            app_deployment_uuid=app_deployment_uuid,
            app_status=app_status,
            helm_status=status,
            logger=logger
        ):
            return

        # Process helm release status
        deployment_status = get_deployment_status_from_helm(status, logger)
        if deployment_status:
            handle_deployment_status(
                name=name,
                namespace=namespace,
                app_uuid=labels.get('shapeblock.com/app-uuid'),
                deployment_uuid=deployment_uuid,
                status=deployment_status,
                helm_status=status,
                logger=logger
            )

    except Exception as e:
        logger.error(f"Failed to process helm release status: {str(e)}")
        # We don't raise here as this is an observation handler

def get_deployment_uuid_from_helm_values(spec: dict) -> Optional[str]:
    """Extract deployment UUID from helm release spec"""
    try:
        return spec['values']['universal-chart']['generic']['labels']['deployUuid']
    except (KeyError, TypeError):
        return None

def get_deployment_uuid_from_app_status(app_status: dict) -> Optional[str]:
    """Extract deployment UUID from application status"""
    try:
        if 'update_app' in app_status:
            return app_status['update_app'].get('lastDeployment')
        return app_status['create_app'].get('lastDeployment')
    except (KeyError, TypeError):
        return None

def should_process_helm_status(deployment_uuid: str, app_deployment_uuid: str,
                             app_status: dict, helm_status: dict, logger) -> bool:
    """Determine if helm status should be processed"""
    # Check if this is the latest deployment
    if deployment_uuid != app_deployment_uuid:
        logger.debug(f"Skipping old deployment {deployment_uuid}")
        return False

    # Check if version already processed
    history = helm_status.get('history', [])
    if not history:
        return False

    current_version = history[0]['version']
    last_processed_version = app_status.get('lastDeployedVersion')

    if last_processed_version == current_version:
        logger.debug(f"Version {current_version} already processed")
        return False

    return True

def get_deployment_status_from_helm(status: dict, logger) -> Optional[str]:
    """Determine deployment status from helm release status"""
    try:
        history = status.get('history', [])
        if not history:
            return None

        latest_status = history[0]['status']
        if latest_status == 'deployed':
            return 'success'
        elif latest_status == 'failed':
            return 'failed'
        return None

    except Exception as e:
        logger.error(f"Failed to determine deployment status: {str(e)}")
        return None

def handle_deployment_status(name: str, namespace: str, app_uuid: str,
                           deployment_uuid: str, status: str,
                           helm_status: dict, logger):
    """Handle deployment status updates"""
    try:
        # Get status message from conditions
        conditions = helm_status.get('conditions', [])
        status_message = conditions[-1]['message'] if conditions else "No status message available"

        # Update deployment status
        logger.info(f"Updating deployment status to {status} for app {app_uuid}")
        data = {
            'logs': status_message,
            'status': status,
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
        }

        # Update backend
        response = requests.post(f"{sb_url}/deployments/", json=data)
        response.raise_for_status()

        # Update application status
        if helm_status.get('history'):
            update_app_deployment_status(
                namespace=namespace,
                name=name,
                deployed_version=helm_status['history'][0]['version'],
                logger=logger
            )

    except Exception as e:
        logger.error(f"Failed to handle deployment status: {str(e)}")

def handle_service_deployment_status(name: str, namespace: str,
                                  service_uuid: str, status: dict, logger):
    """Handle service deployment status updates"""
    try:
        history = status.get('history', [])
        conditions = status.get('conditions', [])

        if not (history and conditions):
            return

        if history[0]['status'] == 'deployed':
            deployment_status = 'success'
        elif history[0]['status'] == 'failed':
            deployment_status = 'failed'
        else:
            return

        logger.info(f"Updating service deployment status {deployment_status} for service {service_uuid}")

        data = {
            'logs': conditions[-1]['message'],
            'status': deployment_status,
            'service_uuid': service_uuid,
        }

        response = requests.post(f"{sb_url}/service-deployments/", json=data)
        response.raise_for_status()

    except Exception as e:
        logger.error(f"Failed to handle service deployment status: {str(e)}")

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
    """Get the Application CR object"""
    try:
        app = k8s_client.execute_with_retry(
            lambda: k8s_client.custom_objects.get_namespaced_custom_object(
                group="dev.shapeblock.com",
                version="v1alpha1",
                name=name,
                namespace=namespace,
                plural="applications",
            )
        )
        return app
    except ApiException as e:
        logger.error(f"Failed to get application {name}: {str(e)}")
        raise

# TODO: daemon to update kpack base images
# TODO: daemon to send status to SB every x hrs

def create_helmrelease(name: str, app_uuid: str, app_spec: Dict, namespace: str, tag: str, logger):
    """Create HelmRelease using template file"""
    try:
        # Prepare chart info
        _, image_tag = tag.split(':')
        chart_info = app_spec.get('chart', {})
        helm_values = chart_info.get('values', {})
        if isinstance(helm_values, str):
            helm_values = yaml.safe_load(helm_values)

        # Update image tag in values
        helm_values['universal-chart']['defaultImageTag'] = image_tag

        # Read template file
        template_path = os.path.join(os.path.dirname(__file__), 'helmrelease2.yaml')
        with open(template_path, 'rt') as f:
            template = f.read()

        # Format template with values
        helm_release_yaml = template.format(
            name=name,
            app_uuid=app_uuid,
            chart_name=chart_info.get('name', 'universal-chart'),
            chart_version=chart_info.get('version', '1.0.0')
        )

        # Parse YAML to dict
        helm_release = yaml.safe_load(helm_release_yaml)

        # Add values to spec
        helm_release['spec']['values'] = helm_values

        logger.debug(f"Creating HelmRelease with spec: {helm_release}")

        k8s_client.execute_with_retry(
            lambda: k8s_client.custom_objects.create_namespaced_custom_object(
                group="helm.toolkit.fluxcd.io",
                version="v2beta2",
                namespace=namespace,
                plural="helmreleases",
                body=helm_release
            )
        )

        logger.info(f"Created HelmRelease {name} in namespace {namespace}")

    except Exception as e:
        logger.error(f"Failed to create HelmRelease: {str(e)}")
        raise

def update_helmrelease(name: str, app_uuid: str, app_spec: Dict, namespace: str, tag: str, logger):
    """Update existing HelmRelease using template"""
    try:
        _, image_tag = tag.split(':')
        # Get current helm release
        current_release = k8s_client.execute_with_retry(
            lambda: k8s_client.custom_objects.get_namespaced_custom_object(
                group="helm.toolkit.fluxcd.io",
                version="v2beta2",
                namespace=namespace,
                name=name,
                plural="helmreleases"
            )
        )

        # Update values
        chart_info = app_spec.get('chart', {})
        helm_values = chart_info.get('values', {})

        if isinstance(helm_values, str):
            helm_values = yaml.safe_load(helm_values)

        helm_values['universal-chart']['defaultImageTag'] = image_tag

        # Read template file
        template_path = os.path.join(os.path.dirname(__file__), 'helmrelease2.yaml')
        with open(template_path, 'rt') as f:
            template = f.read()

        # Format template with values
        helm_release_yaml = template.format(
            name=name,
            app_uuid=app_uuid,
            chart_name=chart_info.get('name', 'universal-chart'),
            chart_version=chart_info.get('version', '1.0.0')
        )

        # Parse YAML to dict
        helm_release = yaml.safe_load(helm_release_yaml)

        # Add values to spec
        helm_release['spec']['values'] = helm_values

        # Prepare patch (only spec)
        patch = {
            'spec': helm_release['spec']
        }

        # Apply patch
        k8s_client.execute_with_retry(
            lambda: k8s_client.custom_objects.patch_namespaced_custom_object(
                group="helm.toolkit.fluxcd.io",
                version="v2beta2",
                namespace=namespace,
                name=name,
                plural="helmreleases",
                body=patch
            )
        )

        logger.info(f"Updated HelmRelease {name} in namespace {namespace}")

    except Exception as e:
        logger.error(f"Failed to update HelmRelease: {str(e)}")
        raise

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

def update_app_status(namespace: str, name: str, tag: Optional[str], logger, error: Optional[str] = None):
    """Update application CR status"""
    try:
        patch_body = {
            'status': {
                'lastTag': tag,
            }
        }

        if error:
            patch_body['status']['lastError'] = error

        k8s_client.execute_with_retry(
            lambda: k8s_client.custom_objects.patch_namespaced_custom_object_status(
                group="dev.shapeblock.com",
                version="v1alpha1",
                namespace=namespace,
                name=name,
                plural="applications",
                body=patch_body
            )
        )

        logger.info(f"Updated application {name} status: tag={tag}, error={error}")

    except Exception as e:
        logger.error(f"Failed to update application status: {str(e)}")
        raise

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

def helmrelease_exists(name: str, namespace: str) -> bool:
    """Check if HelmRelease exists"""
    try:
        k8s_client.execute_with_retry(
            lambda: k8s_client.custom_objects.get_namespaced_custom_object(
                group="helm.toolkit.fluxcd.io",
                version="v2beta2",
                namespace=namespace,
                name=name,
                plural="helmreleases"
            )
        )
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise

def update_status(app_uuid: str, status: str, logs: str, deployment_uuid: str):
    """Update application status in the backend"""
    try:
        data = {
            'logs': logs,
            'status': status,
            'app_uuid': app_uuid,
            'deployment_uuid': deployment_uuid,
        }
        response = requests.post(f"{sb_url}/deployments/", json=data)
        response.raise_for_status()
    except Exception as e:
        kopf.warn(f"Failed to update status: {str(e)}")

def get_deployment_uuid_from_spec(spec: Dict) -> str:
    """Extract deployment UUID from spec"""
    if isinstance(spec['chart']['values'], str):
        chart_values = yaml.safe_load(spec['chart']['values'])
    else:
        chart_values = spec['chart']['values']
    return chart_values['universal-chart']['generic']['labels']['deployUuid']

def get_deployment_uuid_from_status(status: Dict) -> str:
    """Extract deployment UUID from status"""
    if 'update_app' in status:
        return status['update_app'].get('lastDeployment')
    return status['create_app'].get('lastDeployment')

def handle_failed_build(name, namespace, app_uuid, spec, status, deployment_uuid, logger):
    """Handle failed build with improved error detection"""
    try:
        # Get failure reason from conditions
        conditions = status.get('conditions', [])
        failure_reason = next(
            (c.get('message') for c in conditions
             if c.get('type') == 'Succeeded' and c.get('status') == 'False'),
            "Build failed with unknown reason"
        )

        logger.error(f"Build failed: {failure_reason}")

        # Try to get build logs
        build_logs = ""
        if status.get('podName'):
            try:
                # Get pod details first
                pod = k8s_client.execute_with_retry(
                    lambda: k8s_client.core_v1.read_namespaced_pod(
                        namespace=namespace,
                        name=status['podName']
                    )
                )

                # Only try to get logs if pod is running or completed
                if pod.status.phase in ['Running', 'Succeeded', 'Failed']:
                    # Try to get logs from the last step that ran
                    steps_completed = status.get('stepsCompleted', [])
                    if steps_completed:
                        build_logs = k8s_client.execute_with_retry(
                            lambda: k8s_client.core_v1.read_namespaced_pod_log(
                                namespace=namespace,
                                name=status['podName'],
                                container=steps_completed[-1]
                            )
                        )
            except ApiException as e:
                logger.warning(f"Failed to get build logs: {str(e)}")

        # Update status with failure details
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Build failed: {failure_reason}\n\nBuild logs:\n{build_logs}",
            deployment_uuid=deployment_uuid
        )

        # Update application status
        update_app_status(
            namespace=namespace,
            name=name,
            tag=None,
            logger=logger,
            error=failure_reason
        )

    except Exception as e:
        logger.error(f"Error handling build failure: {str(e)}")
        update_status(
            app_uuid=app_uuid,
            status="failed",
            logs=f"Failed to process build failure: {str(e)}",
            deployment_uuid=deployment_uuid
        )

def attach_ssh_secret(name, namespace, service_account, logger):
    """Attach SSH secret to service account for private git repos"""
    try:
        # Get SSH secret from shapeblock namespace
        ssh_secret = k8s_client.execute_with_retry(
            lambda: k8s_client.core_v1.read_namespaced_secret(
                namespace='shapeblock',
                name='git-ssh'
            )
        )

        # Create new secret in app namespace
        secret_name = f"{name}-ssh"
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name),
            data=ssh_secret.data,
            type=ssh_secret.type
        )

        try:
            k8s_client.execute_with_retry(
                lambda: k8s_client.core_v1.create_namespaced_secret(
                    namespace=namespace,
                    body=body
                )
            )
        except ApiException as e:
            if e.status != 409:  # Ignore if already exists
                raise

        # Patch service account to use the secret
        patch_body = {
            "secrets": [{"name": secret_name}]
        }

        k8s_client.execute_with_retry(
            lambda: k8s_client.core_v1.patch_namespaced_service_account(
                name=name,
                namespace=namespace,
                body=patch_body
            )
        )

        logger.info(f"SSH secret {secret_name} attached to service account {name}")

    except Exception as e:
        logger.error(f"Failed to attach SSH secret: {str(e)}")
        raise
