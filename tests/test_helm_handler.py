import pytest
from sb import create_helmrelease, update_helmrelease

def test_create_helmrelease(mock_k8s_client, sample_app_spec):
    """Test creation of HelmRelease"""
    # Setup
    name = 'test-app'
    namespace = 'test-ns'
    app_uuid = 'test-123'
    tag = 'v1.0.0'
    logger = Mock()

    # Execute
    create_helmrelease(
        name=name,
        app_uuid=app_uuid,
        app_spec=sample_app_spec,
        namespace=namespace,
        tag=tag,
        logger=logger
    )

    # Assert
    mock_k8s_client.execute_with_retry.assert_called_with(
        mock.ANY  # Verify the created HelmRelease spec
    )

def test_update_helmrelease(mock_k8s_client, sample_app_spec):
    """Test updating existing HelmRelease"""
    # Setup
    name = 'test-app'
    namespace = 'test-ns'
    app_uuid = 'test-123'
    tag = 'v1.0.1'
    logger = Mock()

    # Mock existing HelmRelease
    mock_k8s_client.execute_with_retry.return_value = {
        'spec': {
            'chart': {
                'spec': {
                    'chart': 'universal-chart',
                    'version': '1.0.0'
                }
            },
            'values': {
                'universal-chart': {
                    'defaultImageTag': 'v1.0.0'
                }
            }
        }
    }

    # Execute
    update_helmrelease(
        name=name,
        app_uuid=app_uuid,
        app_spec=sample_app_spec,
        namespace=namespace,
        tag=tag,
        logger=logger
    )

    # Assert
    assert mock_k8s_client.execute_with_retry.call_count == 2  # Get and patch
