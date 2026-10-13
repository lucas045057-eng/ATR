"""Explicit read-protocol negotiation; the legacy adapter accepts known shapes only."""
from __future__ import annotations

import json

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
