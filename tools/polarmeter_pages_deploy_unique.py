#!/usr/bin/env python3
"""Deploy a GitHub Pages artifact with a unique pages_build_version.

GitHub's official deploy-pages action uses GITHUB_SHA as pages_build_version.
That is fine for static push builds, but PolarMeter cache jobs publish new JSON
artifacts repeatedly from the same commit. The Pages API expects the build
version to be unique per deployment, so this small wrapper uses the uploaded
artifact id plus run metadata to avoid stale public cache serving.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


FINAL_FAILURES = {
    'deployment_failed',
    'deployment_perms_error',
    'deployment_content_failed',
    'deployment_cancelled',
    'deployment_lost',
}


def request_json_once(
    url: str,
    *,
    method: str = 'GET',
    token: str | None = None,
    auth_scheme: str = 'token',
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    headers = {
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2026-03-10',
    }
    if token:
        headers['Authorization'] = f'{auth_scheme} {token}'
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read().decode('utf-8')
    return json.loads(body) if body else {}


def request_json(
    url: str,
    *,
    method: str = 'GET',
    token: str | None = None,
    auth_scheme: str = 'token',
    payload: dict[str, Any] | None = None,
    attempts: int = 4,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return request_json_once(url, method=method, token=token, auth_scheme=auth_scheme, payload=payload)
        except urllib.error.HTTPError as error:
            body = error.read().decode('utf-8', errors='replace')
            last_error = RuntimeError(f'HTTP {error.code} {error.reason} from {url}: {body}')
            if error.code not in {429, 500, 502, 503, 504} or attempt >= attempts:
                raise last_error from error
        except urllib.error.URLError as error:
            last_error = error
            if attempt >= attempts:
                raise RuntimeError(f'network error from {url}: {error}') from error
        time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f'request failed after {attempts} attempts: {last_error}')


def get_oidc_token(audience: str | None = None) -> str:
    request_url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL')
    request_token = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    if not request_url or not request_token:
        raise RuntimeError('ACTIONS_ID_TOKEN_REQUEST_URL/TOKEN missing; set id-token: write')
    if audience:
        separator = '&' if '?' in request_url else '?'
        request_url = f'{request_url}{separator}audience={urllib.parse.quote(audience)}'
    response = request_json(request_url, token=request_token, auth_scheme='Bearer')
    value = response.get('value')
    if not isinstance(value, str) or not value:
        raise RuntimeError('OIDC token response did not contain value')
    return value


def default_build_version() -> str:
    sha = os.environ.get('GITHUB_SHA', 'unknown-sha')
    run_id = os.environ.get('GITHUB_RUN_ID', 'unknown-run')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    seed = f'{sha}-{run_id}-{attempt}'
    return hashlib.sha1(seed.encode('utf-8')).hexdigest()


def write_output(name: str, value: str) -> None:
    output_path = os.environ.get('GITHUB_OUTPUT')
    if not output_path:
        return
    with open(output_path, 'a', encoding='utf-8') as handle:
        handle.write(f'{name}={value}\n')


def deploy_pages(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo or os.environ.get('GITHUB_REPOSITORY')
    token = args.token or os.environ.get('GITHUB_TOKEN')
    if not repo or '/' not in repo:
        raise RuntimeError('GITHUB_REPOSITORY missing or invalid')
    if not token:
        raise RuntimeError('GITHUB_TOKEN missing')
    owner, name = repo.split('/', 1)
    api_root = args.api_url.rstrip('/')
    oidc_token = get_oidc_token(args.audience)
    build_version = args.build_version or default_build_version()
    payload = {
        'artifact_id': int(args.artifact_id),
        'pages_build_version': build_version,
        'oidc_token': oidc_token,
    }
    if args.preview:
        payload['preview'] = True

    create_url = f'{api_root}/repos/{owner}/{name}/pages/deployments'
    redacted_payload = {**payload, 'oidc_token': '***'}
    print(f'Creating Pages deployment with payload: {json.dumps(redacted_payload, ensure_ascii=False)}')
    deployment = request_json(create_url, method='POST', token=token, payload=payload)
    deployment_id = deployment.get('id') or str(deployment.get('status_url', '')).rstrip('/').split('/')[-1] or build_version
    if not deployment_id:
        raise RuntimeError(f'Pages deployment response missing id: {deployment}')
    print(f'Created Pages deployment id={deployment_id} buildVersion={build_version}')

    status_url = f'{api_root}/repos/{owner}/{name}/pages/deployments/{deployment_id}'
    deadline = time.monotonic() + args.timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        status = request_json(status_url, token=token)
        last_status = status
        deployment_status = status.get('status')
        print(f'Pages deployment status: {deployment_status}')
        if deployment_status == 'succeed':
            page_url = status.get('page_url') or deployment.get('page_url') or ''
            write_output('page_url', str(page_url))
            write_output('status', 'succeed')
            write_output('deployment_id', str(deployment_id))
            write_output('pages_build_version', build_version)
            print('Reported success!')
            return status
        if deployment_status in FINAL_FAILURES:
            raise RuntimeError(f'Pages deployment failed with status={deployment_status}: {status}')
        time.sleep(args.poll_seconds)

    raise TimeoutError(f'Pages deployment timed out after {args.timeout_seconds}s: {last_status}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact-id', required=True)
    parser.add_argument('--build-version')
    parser.add_argument('--repo')
    parser.add_argument('--token')
    parser.add_argument('--api-url', default=os.environ.get('GITHUB_API_URL', 'https://api.github.com'))
    parser.add_argument('--audience')
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=5)
    parser.add_argument('--timeout-seconds', type=float, default=600)
    args = parser.parse_args()
    try:
        deploy_pages(args)
    except Exception as exc:
        print(f'PolarMeter unique Pages deploy: FAIL: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
