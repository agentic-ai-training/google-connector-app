"""Fixed probe contract tests; these do not establish live native enforcement."""
import ast

import pytest

from scripts.probe_packaging_native import REMOTE_SOURCE, validate


def test_probe_is_fixed_nonsensitive_and_has_native_cleanup_check():
    tree = ast.parse(REMOTE_SOURCE)
    assert tree
    for required in ('host.require_host_limits()', 'host.verify_native_worker(', 'host.require_native_cleanup(',
                     'on_started=inspect_started', "'--cgroup-parent=gcapackaging.slice'", 'time.sleep(10)'):
        assert required in REMOTE_SOURCE
    assert "'production_packaging_activated':False" in REMOTE_SOURCE
    assert 'request.frame' not in REMOTE_SOURCE and '--mount' not in REMOTE_SOURCE


@pytest.mark.parametrize('verified', [True, False])
def test_verified_negative_observation_is_not_converted_to_success(verified):
    evidence = {'synthetic_cleanup_verified': True, 'production_packaging_activated': False,
                'native_enforcement_verified': verified}
    assert validate(evidence) == evidence


@pytest.mark.parametrize('field,value', [('synthetic_cleanup_verified', False),
                                        ('production_packaging_activated', True),
                                        ('native_enforcement_verified', 1)])
def test_uncertain_or_activated_probe_evidence_is_rejected(field, value):
    evidence = {'synthetic_cleanup_verified': True, 'production_packaging_activated': False,
                'native_enforcement_verified': False, field: value}
    with pytest.raises(RuntimeError, match='unconfirmed'):
        validate(evidence)
