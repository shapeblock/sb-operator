import pytest
import kopf
import kubernetes
from unittest.mock import Mock, patch
import yaml

@pytest.fixture
def mock_k8s_client():
    """Mock the global KubernetesClientManager"""
    with patch('sb.KubernetesClientManager') as mock_manager:
        instance = mock_manager.return_value
        instance.core_v1 = Mock()
        instance.custom_objects = Mock()
        instance.rbac_v1 = Mock()
        yield instance

@pytest.fixture
def mock_requests():
    """Mock requests for API calls to backend"""
    with patch('requests.post') as mock_post:
        mock_post.return_value.status_code = 200
        yield mock_post

@pytest.fixture
def sample_app_spec():
    """Sample application spec"""
    return {
        'git': {
            'repo': 'git@github.com:test/test.git',
            'ref': 'main',
            'subPath': 'app'
        },
        'chart': {
            'name': 'universal-chart',
            'version': '1.0.0',
            'values': {
                'universal-chart': {
                    'generic': {
                        'labels': {
                            'deployUuid': 'test-123'
                        }
                    }
                }
            }
        },
        'tag': 'v1.0.0',
        'stack': 'python'
    }

@pytest.fixture
def sample_build_status():
    """Sample kpack build status"""
    return {
        'conditions': [{
            'type': 'Succeeded',
            'status': 'True',
            'message': 'Build succeeded'
        }],
        'podName': 'test-build-pod',
        'tags': ['registry.shapeblock.com/test:v1.0.0']
    }
