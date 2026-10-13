"""Explicit read-protocol negotiation; the legacy adapter accepts known shapes only."""
from __future__ import annotations

import json
import re

from app.connectors.diagnostics import CapabilityUnavailable, ToolParseError
from app.connectors.remote_reads import PROTOCOL, TOOLS

LEGACY_TOOLS = {'host.info', 'host.memory', 'host.disk', 'process.list', 'network.listeners',
                'service.list', 'service.status', 'service.logs', 'container.list',
                'container.status', 'container.logs', 'config.list', 'config.read'}


def decode(raw):
    try:
        row = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ToolParseError(output_bytes=len(raw)) from exc
    if not isinstance(row, dict):
        raise ToolParseError(output_bytes=len(raw))
    return row


def validate_capabilities(row):
    if (row.get('protocol') != PROTOCOL or type(row.get('schema_version')) is not int
            or row['schema_version'] != 1 or row.get('observation_location') != 'remote_ssh'
            or row.get('time_format') != 'ISO8601-aware'
            or type(row.get('max_output_bytes')) is not int or not 1024 <= row['max_output_bytes'] <= 64000
            or type(row.get('tool_timeout_seconds')) is not int or not 1 <= row['tool_timeout_seconds'] <= 10
            or not isinstance(row.get('tools'), dict) or len(row['tools']) > 40
            or any(not isinstance(k, str) or type(v) is not int for k, v in row['tools'].items())):
        raise ToolParseError()
    return row


def require_tool(row, tool):
    if row.get('tools', {}).get(tool) != TOOLS.get(tool, 1):
        raise CapabilityUnavailable('远端缺少当前工具或工具版本不兼容')


def legacy_host(row):
    text = row.get('text')
    if not isinstance(text, str) or not text.startswith('Linux ') or len(text.splitlines()) != 1 or len(text) > 512:
        raise ToolParseError()
    return {'protocol': 'atr-read-v1-legacy', 'schema_version': 1,
            'tools': {tool: 1 for tool in LEGACY_TOOLS}, 'legacy_adapter': 'tested-json-v1',
            'missing_metadata': ['startup_cycle', 'capability_negotiation', 'remote_health']}


def validate_legacy_payload(tool, row):
    if tool in {'service.status', 'container.status'}:
        if not isinstance(row.get('state'), str) or len(row['state']) > 512:
            raise ToolParseError()
    elif tool.endswith('.list'):
        field = {'service.list': 'services', 'container.list': 'containers', 'process.list': 'processes',
                 'config.list': 'files'}.get(tool)
        if field is None or not isinstance(row.get(field), list) or len(row[field]) > 100:
            raise ToolParseError()
    elif not isinstance(row.get('text'), str):
        raise ToolParseError()
    return row


def validate_payload(tool, args, row, config):
    if (row.get('protocol') != PROTOCOL or row.get('tool_version') != TOOLS.get(tool)
            or row.get('observation_location') != 'remote_ssh'
            or not isinstance(row.get('observed_host'), str) or not 1 <= len(row['observed_host']) <= 253):
        raise ToolParseError()
    if tool in {'container.status', 'container.logs'}:
        if (not isinstance(row.get('container_id'), str) or not row['container_id'].startswith(args['container_id'])
                or row.get('state') not in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'}
                or not isinstance(row.get('startup_cycle'), str)):
            raise ToolParseError()
    if tool in {'service.status', 'service.logs'} and (row.get('name') not in {args['name'], args['name'] + '.service'}
            or row.get('state') not in {'active', 'inactive', 'failed', 'activating', 'deactivating', 'reloading', 'maintenance', 'refreshing'}):
        raise ToolParseError()
    if tool.endswith('.logs') and (not isinstance(row.get('text'), str)
            or not row.get('partial') and row.get('cycle_verified') is not True):
        raise ToolParseError()
    if tool == 'health.get':
        endpoint = next((e for e in config.get('health_endpoints', []) if e['endpoint_id'] == args['endpoint_id']), None)
        if (not endpoint or row.get('endpoint_id') != endpoint['endpoint_id'] or row.get('url') != endpoint['url']
                or type(row.get('status_code')) is not int or not 100 <= row['status_code'] <= 599):
            raise ToolParseError()
    if tool == 'process.list':
        rows=row.get('processes')
        if not isinstance(rows,list) or len(rows)>100:
            raise ToolParseError()
        for item in rows:
            if (not isinstance(item,dict) or type(item.get('pid')) is not int or not 1<=item['pid']<=4194304
                    or not re.fullmatch(r'[a-f0-9-]{36}:[0-9]{1,20}',str(item.get('start_time','')))
                    or item.get('id')!=str(item['pid'])+':'+item['start_time'] or item.get('kind')!='process'
                    or not isinstance(item.get('name'),str) or not 1<=len(item['name'])<=128):
                raise ToolParseError()
    if tool == 'process.status' and (row.get('pid')!=args['pid'] or row.get('start_time')!=args['start_time']
            or row.get('startup_cycle')!=args['start_time'] or row.get('state') not in {'running','exited'}):
        raise ToolParseError()
    if tool == 'log.tail':
        source=next((item for item in config.get('log_files',[]) if item['file_id']==args['file_id']),None)
        if (not source or row.get('file_id')!=source['file_id'] or row.get('pid')!=source['pid']
                or row.get('start_time')!=source['start_time'] or row.get('startup_cycle')!=source['start_time']
                or not isinstance(row.get('text'),str) or not row.get('partial') and row.get('cycle_verified') is not True):
            raise ToolParseError()
    return row
