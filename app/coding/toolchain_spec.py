"""Pure bounded inspection of operator-owned offline dependency specifications.

This does not acquire dependencies, execute code, approve an image or authorize
deployment. Runtime validation still uses its immutable operator image policy.
"""
import base64
import hashlib
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


BASES = {
    'react': 'docker.io/library/node@sha256:5cbc7caba8c2c0f0bca675d1b61b9f2857e1cf1853c6164ee9dd409501a936e7',
    'spring': 'docker.io/library/maven@sha256:c97d0fa42bed8c381518d76d7ca541ff5c6427b21fa93ac6fc4bd68ffb9a258d',
}
ROLES = {'react': 'react_offline_v1', 'spring': 'spring_offline_v1'}
TAGS = {'react': 'gca-reviewed-react:offline-v1', 'spring': 'gca-reviewed-spring:offline-v1'}
# Operator-reviewed acquisition and repeated tagless-export evidence, not runtime
# approval. A changed recipe/image requires new measured evidence and source review.
REVIEWED_ARCHIVES = {
    'react': {
        'image_id': 'sha256:d3d2984e4d4b8e90a5b93b0f3a89591c8958fd42a8d8f68de4ac5eafaeeba9de',
        'archive_sha256': 'bbe2dde5593ef0fff2d589f8fe9de801aa02d601ddc66992d3cf2a3550eeed07',
        'archive_bytes': 108749312,
        'recipe_sha256': 'cca456298dd4242ff67f662b3b8cc7b94377a8faefe5ec4d49d536baba4adb66',
    },
    'spring': {
        'image_id': 'sha256:1f90c0fe3495fc3cc00f8b3a9cc6681782238fa1f1dd8a57c8583bac31c68943',
        'archive_sha256': '14f01f1dfcbe010f885012f01b56c583552935304310c9a31a35928462706595',
        'archive_bytes': 509887488,
        'recipe_sha256': 'dfb691f3330ec05842b6f5b53b60cbabfaae6aa9179215a0e7418cb1d6a4686a',
    },
}
MAX_SPEC_BYTES = 512 * 1024
REACT_DEPENDENCIES = {'react': '19.3.0', 'react-dom': '19.3.0'}
REACT_DEV_DEPENDENCIES = {'@vitejs/plugin-react': '6.1.2', 'vite': '8.3.2', 'vitest': '5.0.3'}
SPRING_DEPENDENCIES = {
    ('org.springframework.boot', 'spring-boot-starter-webmvc', ''),
    ('org.springframework.boot', 'spring-boot-starter-security', ''),
    ('org.springframework.boot', 'spring-boot-starter-data-jpa', ''),
    ('org.postgresql', 'postgresql', 'runtime'),
    ('com.h2database', 'h2', 'test'),
    ('org.springframework.boot', 'spring-boot-starter-test', 'test'),
    ('org.springframework.security', 'spring-security-test', 'test'),
}


class ToolchainSpecError(ValueError):
    pass


def bounded(body):
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_SPEC_BYTES:
        raise ToolchainSpecError('dependency specification exceeds bound')
    return body


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ToolchainSpecError('duplicate dependency JSON key')
        result[key] = value
    return result


def inspect_react(manifest_body, lock_body):
    manifest = json.loads(bounded(manifest_body), object_pairs_hook=unique_object)
    lock = json.loads(bounded(lock_body), object_pairs_hook=unique_object)
    if (not isinstance(manifest, dict) or set(manifest) - {
            'name', 'version', 'private', 'type', 'dependencies', 'devDependencies'}
            or manifest.get('private') is not True or manifest.get('type') != 'module'
            or not isinstance(lock, dict) or lock.get('lockfileVersion') != 3
            or not isinstance(lock.get('packages'), dict) or not 2 <= len(lock['packages']) <= 1000):
        raise ToolchainSpecError('unsupported or executable dependency specification')
    if (manifest.get('dependencies') != REACT_DEPENDENCIES
            or manifest.get('devDependencies') != REACT_DEV_DEPENDENCIES):
        raise ToolchainSpecError('dependency role/version changes require separate review')
    packages = lock['packages']
    root = packages.get('')
    if not isinstance(root, dict):
        raise ToolchainSpecError('dependency lock has no root')
    for kind in ('dependencies', 'devDependencies'):
        declared = manifest.get(kind, {})
        if not isinstance(declared, dict) or root.get(kind, {}) != declared:
            raise ToolchainSpecError('dependency lock differs from manifest')
        for name, version in declared.items():
            if (not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
                    or packages.get('node_modules/' + name, {}).get('version') != version):
                raise ToolchainSpecError('direct dependency must have an exact locked stable version')
    for path, entry in packages.items():
        if not path:
            continue
        relative = PurePosixPath(path)
        if (not path.startswith('node_modules/') or relative.is_absolute()
                or any(part in {'..', '.'} for part in path.split('/'))
                or relative.as_posix() != path or not isinstance(entry, dict)
                or entry.get('link') or not isinstance(entry.get('resolved'), str)
                or not isinstance(entry.get('integrity'), str)):
            raise ToolchainSpecError('dependency lock has unsupported path or reference')
        source = urlsplit(entry['resolved'])
        if (source.scheme != 'https' or source.hostname != 'registry.npmjs.org'
                or source.port not in {None, 443} or source.username or source.password
                or source.query or source.fragment or not source.path.endswith('.tgz')):
            raise ToolchainSpecError('dependency acquisition requires the public approved registry')
        integrity = entry['integrity']
        try:
            checksum = base64.b64decode(integrity.removeprefix('sha512-'), validate=True)
        except ValueError:
            raise ToolchainSpecError('dependency integrity is malformed') from None
        if not integrity.startswith('sha512-') or len(checksum) != 64:
            raise ToolchainSpecError('dependency integrity must bind SHA-512 content')
    return {'role': ROLES['react'], 'package_count': len(packages) - 1,
            'manifest_sha256': hashlib.sha256(manifest_body).hexdigest(),
            'lock_sha256': hashlib.sha256(lock_body).hexdigest(),
            'lifecycle_scripts_allowed': False, 'acquisition_registry': 'https://registry.npmjs.org'}


def inspect_spring(pom_body):
    bounded(pom_body)
    if b'<!DOCTYPE' in pom_body.upper() or b'<!ENTITY' in pom_body.upper():
        raise ToolchainSpecError('dependency XML entities are forbidden')
    # XML entities/doctype rejected before bounded parsing.
    root = ET.fromstring(pom_body)  # nosec B314
    prefix = '{http://maven.apache.org/POM/4.0.0}'
    if root.tag != prefix + 'project':
        raise ToolchainSpecError('dependency POM namespace differs')
    # This is a reviewed role-specific catalog, not a generated POM execution API.
    forbidden = {'repositories', 'pluginRepositories', 'profiles', 'extensions', 'reporting',
                 'executions', 'configuration', 'systemPath', 'modules', 'distributionManagement'}
    if any(node.tag.removeprefix(prefix) in forbidden for node in root.iter()):
        raise ToolchainSpecError('additional repository/plugin authority requires separate review')
    parent = root.find(prefix + 'parent')
    if (parent is None or len(root.findall(prefix + 'parent')) != 1
            or parent.find(prefix + 'relativePath') is None
            or (parent.findtext(prefix + 'relativePath') or '').strip()
            or parent.findtext(prefix + 'groupId') != 'org.springframework.boot'
            or parent.findtext(prefix + 'artifactId') != 'spring-boot-starter-parent'
            or parent.findtext(prefix + 'version') != '4.1.1'
            or root.findtext(prefix + 'properties/' + prefix + 'java.version') != '17'):
        raise ToolchainSpecError('stable Spring Boot/Java 17 parent is required')
    plugins = root.findall(prefix + 'build/' + prefix + 'plugins/' + prefix + 'plugin')
    allowed = {('org.springframework.boot', 'spring-boot-maven-plugin'),
               ('org.apache.maven.plugins', 'maven-dependency-plugin')}
    observed = {(p.findtext(prefix + 'groupId'), p.findtext(prefix + 'artifactId')) for p in plugins}
    if observed != allowed or len(plugins) != len(allowed):
        raise ToolchainSpecError('only the reviewed dependency and Boot plugins are allowed')
    for plugin in plugins:
        expected_version = ('3.11.0' if plugin.findtext(prefix + 'artifactId') ==
                            'maven-dependency-plugin' else None)
        if (plugin.findtext(prefix + 'version') != expected_version
                or any(node.tag not in {prefix + 'groupId', prefix + 'artifactId', prefix + 'version'}
                       for node in plugin)):
            raise ToolchainSpecError('plugin version/authority changes require separate review')
    dependencies = root.findall(prefix + 'dependencies/' + prefix + 'dependency')
    actual = set()
    for dependency in dependencies:
        if any(node.tag not in {prefix + 'groupId', prefix + 'artifactId', prefix + 'scope'}
               for node in dependency):
            raise ToolchainSpecError('dependency authority changes require separate review')
        actual.add((dependency.findtext(prefix + 'groupId'), dependency.findtext(prefix + 'artifactId'),
                    dependency.findtext(prefix + 'scope') or ''))
    if actual != SPRING_DEPENDENCIES or len(dependencies) != len(actual):
        raise ToolchainSpecError('dependency role changes require separate review')
    if (len(root.findall(prefix + 'build')) != 1
            or len(root.findall(prefix + 'properties')) != 1
            or any(node.tag != prefix + 'java.version' for node in root.find(prefix + 'properties'))
            or any(node.tag not in {prefix + value for value in (
                'modelVersion', 'parent', 'groupId', 'artifactId', 'version', 'properties',
                'dependencies', 'build')} for node in root)):
        raise ToolchainSpecError('additional POM authority requires separate review')
    build = root.find(prefix + 'build')
    if (len(build.findall(prefix + 'plugins')) != 1
            or any(node.tag != prefix + 'plugins' for node in build)
            or any(node.tag != prefix + 'plugin' for node in build.find(prefix + 'plugins'))
            or len(root.findall(prefix + 'dependencies')) != 1
            or any(node.tag != prefix + 'dependency' for node in root.find(prefix + 'dependencies'))):
        raise ToolchainSpecError('additional build/dependency authority requires separate review')
    for node in root.iter():
        if node is root:
            continue
        if node.attrib:
            raise ToolchainSpecError('dependency XML attributes require separate review')
        for name in ('groupId', 'artifactId', 'version', 'scope', 'relativePath', 'java.version'):
            children = node.findall(prefix + name)
            if len(children) > 1 or any(len(child) for child in children):
                raise ToolchainSpecError('duplicate/nested dependency fields require separate review')
    return {'role': ROLES['spring'], 'pom_sha256': hashlib.sha256(pom_body).hexdigest(),
            'java_version': 17, 'spring_boot_version': parent.findtext(prefix + 'version'),
            'go_offline_is_not_validation': True}
