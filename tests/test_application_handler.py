import pytest
from kubernetes.client.rest import ApiException
from sb import create_app, BuildStatus, DeployStatus, AppStatus

def test_create_app_success(mock_k8s_client, mock_requests, sample_app_spec):
    """Test successful application creation"""
    # Setup
    name = 'test-app'
    namespace = 'test-ns'
    labels = {'shapeblock.com/app-uuid': 'test-123'}
    logger = Mock()

    # Mock successful service account creation
    mock_k8s_client.execute_with_retry.return_value = {'metadata': {'name': name}}

    # Execute
    result = create_app(
        spec=sample_app_spec,
        name=name,
        namespace=namespace,
        labels=labels,
        logger=logger
    )

    # Assert
    assert result == {'lastDeployment': 'test-123'}
    assert mock_k8s_client.execute_with_retry.call_count >= 2  # Service account and builder creation
    mock_requests.assert_called_with(
        'https://api.shapeblock.com/deployments/',
        json={
            'logs': 'Image created successfully',
            'status': 'running',
            'app_uuid': 'test-123',
            'deployment_uuid': 'test-123'
        }
    )

def test_create_app_builder_failure(mock_k8s_client, mock_requests, sample_app_spec):
    """Test handling of builder creation failure"""
    # Setup
    name = 'test-app'
    namespace = 'test-ns'
    labels = {'shapeblock.com/app-uuid': 'test-123'}
    logger = Mock()

    # Mock builder creation failure
    mock_k8s_client.execute_with_retry.side_effect = [
        {'metadata': {'name': name}},  # Service account success
        ApiException(status=500)  # Builder failure
    ]

    # Execute and assert
    with pytest.raises(kopf.TemporaryError) as exc:
        create_app(
            spec=sample_app_spec,
            name=name,
            namespace=namespace,
            labels=labels,
            logger=logger
        )

    assert 'Builder creation failed' in str(exc.value)
    mock_requests.assert_called_with(
        'https://api.shapeblock.com/deployments/',
        json={
            'logs': mock.ANY,  # Check contains error message
            'status': 'failed',
            'app_uuid': 'test-123',
            'deployment_uuid': 'test-123'
        }
    )
