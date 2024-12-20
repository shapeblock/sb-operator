import pytest
from sb import trigger_helm_release, handle_successful_build, handle_failed_build

def test_successful_build_handling(mock_k8s_client, mock_requests, sample_build_status):
    """Test handling of successful build"""
    # Setup
    name = 'test-app'
    namespace = 'test-ns'
    labels = {
        'shapeblock.com/app-uuid': 'test-123',
        'image.kpack.io/image': 'test-app'
    }
    logger = Mock()

    # Mock app status retrieval
    mock_k8s_client.execute_with_retry.return_value = {
        'status': {
            'create_app': {
                'lastDeployment': 'test-123'
            }
        }
    }

    # Execute
    trigger_helm_release(
        name=name,
        namespace=namespace,
        labels=labels,
        spec={'tags': ['old-tag', 'new-tag']},
        status=sample_build_status,
        new=[{
            'type': 'Succeeded',
            'status': 'True'
        }],
        logger=logger
    )

    # Assert
    assert mock_k8s_client.execute_with_retry.called
    mock_requests.assert_called_with(
        'https://api.shapeblock.com/deployments/',
        json={
            'logs': 'Build completed successfully, initiating deployment',
            'status': 'running',
            'app_uuid': 'test-123',
            'deployment_uuid': 'test-123'
        }
    )

def test_failed_build_handling(mock_k8s_client, mock_requests):
    """Test handling of failed build"""
    # Setup
    name = 'test-app'
    namespace = 'test-ns'
    app_uuid = 'test-123'
    deployment_uuid = 'test-123'
    logger = Mock()

    failed_status = {
        'conditions': [{
            'type': 'Succeeded',
            'status': 'False',
            'message': 'Build failed: compilation error'
        }],
        'podName': 'test-build-pod'
    }

    # Mock pod logs
    mock_k8s_client.core_v1.read_namespaced_pod_log.return_value = 'Build log output'

    # Execute
    handle_failed_build(
        name=name,
        namespace=namespace,
        app_uuid=app_uuid,
        spec={},
        status=failed_status,
        deployment_uuid=deployment_uuid,
        logger=logger
    )

    # Assert
    mock_requests.assert_called_with(
        'https://api.shapeblock.com/deployments/',
        json={
            'logs': mock.ANY,  # Should contain both error message and build logs
            'status': 'failed',
            'app_uuid': 'test-123',
            'deployment_uuid': 'test-123'
        }
    )
