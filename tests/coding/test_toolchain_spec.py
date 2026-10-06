"""Real checked-in specs and malicious authority expansion rejection; no Docker."""
import base64
import copy
import io
import json
from pathlib import Path
import tarfile

import pytest

from app.coding import toolchain_spec as spec
from scripts.probe_local_offline_toolchains import fixture_archive


ROOT = Path(__file__).resolve().parents[2]


def react():
    directory = ROOT / 'toolchains/react'
    return json.loads((directory / 'package.json').read_bytes()), json.loads((directory / 'package-lock.json').read_bytes())


def inspect_react(manifest, lock):
    return spec.inspect_react(json.dumps(manifest).encode(), json.dumps(lock).encode())


def test_checked_in_specs_and_fixed_source_archives_are_real_and_bounded():
    for role in spec.ROLES:
        archive, evidence = fixture_archive(role)
        assert len(archive) < 128000
        assert len(evidence['fixture_sha256']) == 64
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            names = stream.getnames()
            assert all(item.isfile() for item in stream)
            assert not any('.env' in name or 'node_modules' in name or 'target/' in name for name in names)
        assert evidence['role'] == spec.ROLES[role]
    manifest, lock = react()
    assert inspect_react(manifest, lock)['package_count'] > 30


@pytest.mark.parametrize('kind', ['script', 'range', 'lock_mismatch', 'wrong_integrity', 'link',
                                 'file_url', 'private_registry', 'credentials', 'traversal'])
def test_npm_acquisition_cannot_expand_execution_network_or_path_authority(kind):
    manifest, lock = react()
    entry = lock['packages']['node_modules/react']
    if kind == 'script':
        manifest['scripts'] = {'postinstall': 'unsafe'}
    elif kind == 'range':
        manifest['dependencies']['react'] = '^19.3.0'
        lock['packages']['']['dependencies']['react'] = '^19.3.0'
    elif kind == 'lock_mismatch':
        entry['version'] = '0.0.0'
    elif kind == 'wrong_integrity':
        entry['integrity'] = 'sha512-' + base64.b64encode(b'wrong').decode()
    elif kind == 'link':
        entry['link'] = True
    elif kind == 'file_url':
        entry['resolved'] = 'file:///private/package.tgz'
    elif kind == 'private_registry':
        entry['resolved'] = 'https://private.example/package.tgz'
    elif kind == 'credentials':
        entry['resolved'] = 'https://secret@registry.npmjs.org/package.tgz'
    else:
        lock['packages']['node_modules/../private'] = copy.deepcopy(entry)
    with pytest.raises(spec.ToolchainSpecError):
        inspect_react(manifest, lock)


@pytest.mark.parametrize('element', ['repositories', 'pluginRepositories', 'profiles', 'extensions',
                                   'executions', 'configuration', 'systemPath', 'modules'])
def test_maven_dependency_specification_cannot_expand_plugin_or_repository_authority(element):
    pom = (ROOT / 'toolchains/spring/pom.xml').read_bytes()
    changed = pom.replace(b'</project>', ('<' + element + '>unsafe</' + element + '></project>').encode())
    with pytest.raises(spec.ToolchainSpecError, match='separate review'):
        spec.inspect_spring(changed)


def test_xml_entity_and_specification_budgets_rejected():
    with pytest.raises(spec.ToolchainSpecError, match='entities'):
        spec.inspect_spring(b'<!DOCTYPE foo [<!ENTITY secret SYSTEM "file:///private">]><foo/>')
    with pytest.raises(spec.ToolchainSpecError, match='bound'):
        spec.inspect_spring(b'x' * (spec.MAX_SPEC_BYTES + 1))


def test_duplicate_json_keys_cannot_hide_executable_fields_or_dependency_versions():
    manifest, lock = react()
    encoded = json.dumps(manifest).replace('"private": true', '"private": false, "private": true')
    with pytest.raises(spec.ToolchainSpecError, match='duplicate'):
        spec.inspect_react(encoded.encode(), json.dumps(lock).encode())


@pytest.mark.parametrize('kind', ['version', 'plugin_dependency', 'unknown_dependency', 'duplicate_dependency',
                                 'dependency_version', 'unknown_property', 'boot_version',
                                 'default_goal', 'duplicate_plugin_version', 'attribute'])
def test_fixed_spring_role_rejects_unreviewed_dependency_or_plugin_expansion(kind):
    pom = (ROOT / 'toolchains/spring/pom.xml').read_bytes()
    if kind == 'version':
        pom = pom.replace(b'<version>3.11.0</version>', b'<version>3.12.0</version>')
    elif kind == 'plugin_dependency':
        pom = pom.replace(b'</plugin>', b'<dependencies><dependency/></dependencies></plugin>', 1)
    elif kind == 'unknown_dependency':
        pom = pom.replace(b'<artifactId>postgresql</artifactId>', b'<artifactId>unknown</artifactId>')
    elif kind == 'duplicate_dependency':
        pom = pom.replace(b'</dependencies>', b'<dependency><groupId>org.postgresql</groupId>'
                          b'<artifactId>postgresql</artifactId><scope>runtime</scope></dependency></dependencies>')
    elif kind == 'dependency_version':
        pom = pom.replace(b'<scope>runtime</scope>', b'<scope>runtime</scope><version>1.0.0</version>')
    elif kind == 'unknown_property':
        pom = pom.replace(b'</properties>', b'<unreviewed>unsafe</unreviewed></properties>')
    elif kind == 'boot_version':
        pom = pom.replace(b'<version>4.1.1</version>', b'<version>4.2.0-M2</version>')
    elif kind == 'default_goal':
        pom = pom.replace(b'<build>', b'<build><defaultGoal>unsafe</defaultGoal>')
    elif kind == 'duplicate_plugin_version':
        pom = pom.replace(b'<version>3.11.0</version>', b'<version>3.11.0</version><version>3.12.0</version>')
    else:
        pom = pom.replace(b'<dependency>', b'<dependency unreviewed="unsafe">', 1)
    with pytest.raises(spec.ToolchainSpecError):
        spec.inspect_spring(pom)


def test_negative_fixture_is_exactly_one_test_mutation_with_distinct_source_binding():
    from scripts.probe_local_offline_toolchains import FAILURE_MARKER
    for role in spec.ROLES:
        positive, first = fixture_archive(role)
        negative, second = fixture_archive(role, negative=True)
        assert first['fixture_sha256'] != second['fixture_sha256']
        with tarfile.open(fileobj=io.BytesIO(positive)) as archive:
            original = {item.name: archive.extractfile(item).read() for item in archive}
        with tarfile.open(fileobj=io.BytesIO(negative)) as archive:
            changed = {item.name: archive.extractfile(item).read() for item in archive}
        assert original.keys() == changed.keys()
        differences = [name for name in original if original[name] != changed[name]]
        assert len(differences) == 1
        assert FAILURE_MARKER.encode() in changed[differences[0]]


def test_recipes_only_copy_dependency_specs_and_pin_bases_without_customer_mounts():
    for role, base in spec.BASES.items():
        directory = ROOT / 'toolchains' / role
        source = (directory / 'Dockerfile').read_text()
        assert base in source and 'USER 65532:65532' in source
        assert 'ADD ' not in source and '--mount' not in source
        if role == 'react':
            assert 'src/' not in source
        else:
            assert 'COPY seed/ ./src/' in source and 'rm -rf src target' in source
            for path in (directory / 'seed').rglob('*.java'):
                fixture = ROOT / 'tests/fixtures/coding_toolchains/backend/src' / path.relative_to(directory / 'seed')
                assert path.read_bytes() == fixture.read_bytes()
        assert (directory / '.dockerignore').read_text().startswith('*\n')
    assert '--ignore-scripts' in (ROOT / 'toolchains/react/Dockerfile').read_text()
