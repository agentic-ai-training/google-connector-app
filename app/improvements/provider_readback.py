"""Independent bounded provider reads. No deployment/model/cleanup authority.

Credentials come only from explicit server environment, never callback bodies,
candidate files, broad legacy-account fallbacks or model-visible configuration.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from app.coding.ci_trust import CITrustError

RAILWAY_API = 'https://backboard.railway.com/graphql/v2'
RAILWAY_QUERY = '''query CandidateReadback($deployment:String!,$service:String!,$environment:String!){
  projectToken { projectId environmentId }
  deployment(id:$deployment){id projectId serviceId environmentId status createdAt meta
    service{id name} instances{id status}}
  serviceInstance(serviceId:$service,environmentId:$environment){serviceId environmentId
    activeDeployments{id status} domains{serviceDomains{domain} customDomains{domain}}}
  deploymentLogs(deploymentId:$deployment,limit:200){message}
}'''


@dataclass(frozen=True)
class ProviderConfig:
    railway_token: str = field(default='', repr=False)
    railway_environment: str = ''
    railway_service: str = ''
    vercel_token: str = field(default='', repr=False)
    vercel_team: str = ''
    vercel_project: str = ''

    def target(self, surfaces):
        values = {'railway_environment_id': self.railway_environment, 'railway_service_id': self.railway_service}
        if not self.railway_token:
            raise CITrustError('dedicated_environment_scoped_railway_readback_token')
        if 'frontend' in surfaces:
            values.update({'vercel_team_id': self.vercel_team, 'vercel_project_id': self.vercel_project})
            if not self.vercel_token:
                raise CITrustError('dedicated_vercel_readback_token')
        if any(not re.fullmatch('[A-Za-z0-9_-]{1,200}', value) for value in values.values()):
            raise CITrustError('explicit_immutable_provider_target_ids')
        return values


def load_config():
    return ProviderConfig(railway_token=os.getenv('RAILWAY_CANDIDATE_PROJECT_TOKEN', ''),
        railway_environment=os.getenv('RAILWAY_CANDIDATE_ENVIRONMENT_ID', ''),
        railway_service=os.getenv('RAILWAY_CANDIDATE_SERVICE_ID', ''),
        vercel_token=os.getenv('VERCEL_CANDIDATE_READBACK_TOKEN', ''),
        vercel_team=os.getenv('VERCEL_CANDIDATE_TEAM_ID', ''),
        vercel_project=os.getenv('VERCEL_CANDIDATE_PROJECT_ID', ''))


@dataclass(frozen=True)
class Observation:
    evidence: dict
    observed_monotonic: float = field(default_factory=time.monotonic)

    def require_fresh(self):
        if not 0 <= time.monotonic() - self.observed_monotonic <= 30 or self.evidence.get('status') != 'passed':
            raise CITrustError('fresh_independent_provider_observation')
        return self.evidence


async def _json(client, method, url, *, limit=1_000_000, **kwargs):
    async with client.stream(method, url, **kwargs) as response:
        if response.status_code != 200:
            raise CITrustError('successful_independent_provider_read')
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise CITrustError('bounded_independent_provider_response')
    value = json.loads(body)
    if not isinstance(value, dict):
        raise CITrustError('valid_independent_provider_response')
    return value


def _origin(value, suffix):
    parsed = urlsplit(value or '')
    if (parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith(suffix)
            or not re.fullmatch(r'[a-zA-Z0-9.-]+', parsed.hostname)
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
            or value.rstrip('/') != 'https://' + parsed.hostname):
        raise CITrustError('provider_owned_credential_free_health_origin')
    return 'https://' + parsed.hostname


def _created(value, claimed_at):
    if type(value) is int:
        stamp = datetime.fromtimestamp(value / 1000, timezone.utc)
    elif isinstance(value, str):
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    else:
        raise CITrustError('provider_deployment_creation_time')
    if stamp.tzinfo is None or not claimed_at or stamp < claimed_at or (stamp-datetime.now(timezone.utc)).total_seconds() > 120:
        raise CITrustError('new_deployment_created_after_bound_claim')


async def _health(client, origin, version, *, frontend=False):
    # No provider/callback token on application requests, redirects or proxy use.
    value = await _json(client, 'GET', origin + ('/api/frontend-health' if frontend else '/health'), limit=16000)
    if (value.get('status') != 'ok' or value.get('executor_role') != 'candidate'
            or value.get('deployment_version' if frontend else 'executor_version') != version):
        raise CITrustError('version_bound_independent_application_health')


async def verify(config, frozen, evidence, *, claimed_at):
    """Read exact Railway target/instances/image and optional Vercel preview.

    Upload metadata and application readiness are not cryptographic source/build
    provenance or a complete behavioral test. Never retain raw logs/meta/secrets.
    """
    target = config.target(frozen['runtime_surfaces'])
    if target != frozen.get('provider_target'):
        raise CITrustError('unchanged_provider_readback_target')
    if (evidence.get('candidate_version') != frozen['candidate_version']
            or evidence.get('project_id') != frozen['project_id']
            or evidence.get('service_name') != frozen['service_name']
            or sorted(evidence.get('runtime_surfaces') or []) != frozen['runtime_surfaces']):
        raise CITrustError('provider_evidence_bound_to_frozen_source_and_target')
    deployment_id = evidence.get('deployment_id', '')
    if not isinstance(deployment_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,200}', deployment_id):
        raise CITrustError('immutable_provider_deployment_identifier')
    try:
        async with asyncio.timeout(40), httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
            raw = await _json(client, 'POST', RAILWAY_API,
                headers={'Project-Access-Token': config.railway_token}, json={'query': RAILWAY_QUERY,
                    'variables': {'deployment': deployment_id, 'service': config.railway_service,
                        'environment': config.railway_environment}})
            if raw.get('errors') or not isinstance(raw.get('data'), dict):
                raise CITrustError('complete_railway_readback_without_graphql_errors')
            data = raw['data']
            scope, deployment, instance = data['projectToken'], data['deployment'], data['serviceInstance']
            if (scope.get('projectId') != frozen['project_id'] or scope.get('environmentId') != config.railway_environment
                    or deployment.get('id') != deployment_id or deployment.get('status') != 'SUCCESS'
                    or deployment.get('projectId') != frozen['project_id']
                    or deployment.get('serviceId') != config.railway_service
                    or deployment.get('environmentId') != config.railway_environment
                    or deployment.get('service', {}).get('id') != config.railway_service
                    or deployment.get('service', {}).get('name') != frozen['service_name']
                    or instance.get('serviceId') != config.railway_service
                    or instance.get('environmentId') != config.railway_environment):
                raise CITrustError('successful_exact_scoped_railway_deployment')
            _created(deployment.get('createdAt'), claimed_at)
            digest = deployment.get('meta', {}).get('imageDigest', '')
            if not re.fullmatch('sha256:[0-9a-f]{64}', digest) or digest != evidence.get('image_digest'):
                raise CITrustError('independently_observed_immutable_image_digest')
            replicas = deployment.get('instances')
            if (not isinstance(replicas, list) or not 1 <= len(replicas) <= 20
                    or any(not item.get('id') or item.get('status') != 'RUNNING' for item in replicas)
                    or instance.get('activeDeployments') != [{'id': deployment_id, 'status': 'SUCCESS'}]):
                raise CITrustError('only_approved_deployment_active_with_running_instances')
            logs = data.get('deploymentLogs')
            if not isinstance(logs, list) or len(logs) > 200 or not any(
                    isinstance(item.get('message'), str)
                    and any(prefix in item['message'] for prefix in ('candidate_runtime_ready role=candidate', 'worker_ready role=candidate'))
                    and f"executor_version={frozen['candidate_version']}" in item['message'] for item in logs):
                raise CITrustError('independently_observed_version_bound_runtime_readiness')
            domains = instance.get('domains') or {}
            service_domains, custom_domains = domains.get('serviceDomains'), domains.get('customDomains')
            if not isinstance(service_domains, list) or not isinstance(custom_domains, list) or len(service_domains) > 10 or len(custom_domains) > 10:
                raise CITrustError('bounded_provider_domain_evidence')
            origin = None
            if 'api' in frozen['runtime_surfaces']:
                origin = _origin(evidence.get('deployment_url'), '.up.railway.app')
                if urlsplit(origin).hostname not in {item['domain'] for item in service_domains}:
                    raise CITrustError('api_origin_owned_by_exact_provider_target')
                await _health(client, origin, frozen['candidate_version'])
            elif service_domains or custom_domains or evidence.get('deployment_url'):
                raise CITrustError('worker_only_candidate_has_no_public_domains')
            observed = {'status': 'passed', 'candidate_version': frozen['candidate_version'],
                'railway': {'deployment_id': deployment_id, 'project_id': frozen['project_id'],
                    'service_id': config.railway_service, 'environment_id': config.railway_environment,
                    'image_digest': digest, 'running_instances': len(replicas), 'readiness_observed': True,
                    'origin': origin}, 'source_provenance_is_separate': True}
            if 'frontend' in frozen['runtime_surfaces']:
                front_id = evidence.get('frontend_deployment_id') or ''
                if not re.fullmatch('[A-Za-z0-9_-]{1,200}', front_id):
                    raise CITrustError('immutable_vercel_deployment_identifier')
                front = await _json(client, 'GET', 'https://api.vercel.com/v13/deployments/' + quote(front_id, safe=''),
                    headers={'Authorization': 'Bearer ' + config.vercel_token}, params={'teamId': config.vercel_team})
                meta = front.get('meta') or {}
                front_origin = _origin(evidence.get('frontend_url'), '.vercel.app')
                if (front.get('id') != front_id or front.get('projectId') != config.vercel_project
                        or front.get('target') == 'production' or front.get('readyState') != 'READY'
                        or front.get('url') != urlsplit(front_origin).hostname
                        or meta.get('candidateVersion') != frozen['candidate_version']
                        or meta.get('proposalKey') != frozen['proposal_key']
                        or meta.get('githubCommitSha', frozen['candidate_version']) != frozen['candidate_version']):
                    raise CITrustError('ready_exact_project_version_bound_vercel_preview')
                _created(front.get('createdAt'), claimed_at)
                await _health(client, front_origin, frozen['candidate_version'], frontend=True)
                observed['vercel'] = {'deployment_id': front_id, 'project_id': config.vercel_project,
                    'team_id': config.vercel_team, 'origin': front_origin, 'ready_state': 'READY'}
            observed['observed_at'] = datetime.now(timezone.utc).isoformat()
            return Observation(observed)
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise CITrustError('available_complete_independent_provider_evidence') from None


async def verify_for_intent(pool, identity, evidence, *, expected_state='claimed', proposal):
    from app.improvements.deployment_intents import status
    row = (await status(pool, identity))['intent']
    if row['state'] != expected_state or row['claimed_at'] is None:
        raise CITrustError('matching_claimed_or_attested_deployment_intent')
    frozen = json.loads(row['frozen']) if isinstance(row['frozen'], str) else row['frozen']
    if (row['proposal_id'] != proposal['id'] or frozen['proposal_hash'] != proposal['content_hash']
            or frozen['proposal_key'] != proposal['proposal_key']):
        raise CITrustError('provider_observation_bound_to_current_proposal')
    return await verify(load_config(), frozen, evidence, claimed_at=row['claimed_at'])


async def verify_registered(pool, proposal):
    evidence = proposal.get('deployment_evidence') or {}
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    if not evidence.get('intent_id'):
        raise CITrustError('retained_attested_deployment_intent_before_traffic_activation')
    return await verify_for_intent(pool, evidence['intent_id'], evidence, expected_state='attested', proposal=proposal)
