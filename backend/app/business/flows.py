"""Source-backed call paths. Static branches never assert observed execution."""
from __future__ import annotations

import posixpath
from collections import defaultdict
from pathlib import PurePosixPath

from app.common import digest


def source_ref(row, reference, version):
    return {**reference, 'schema_version': 'source-ref-1', 'file_id': row['id'],
            'original_artifact_id': row.get('artifact_id'), 'container_id': row.get('artifact_id'),
            'archive_path': row['path'], 'path': row['path'], 'content_digest': row['content_digest'],
            'digest_basis': 'original_bytes', 'profile_version': version, 'source_type': 'Static'}


def build_flows(files, version):
    code = [row for row in files if row.get('media_type') == 'code' and row['analysis'].get('functions')]
    modules, by_path, symbols = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in code:
        module = str(PurePosixPath(row['path']).with_suffix('')).replace('/', '.')
        modules[module].append(row)
        by_path[str(PurePosixPath(row['path']).with_suffix(''))].append(row)
        if row['analysis'].get('package'):
            modules[row['analysis']['package'] + '.' + PurePosixPath(row['path']).stem].append(row)
        for function in row['analysis']['functions']:
            symbols[function['name']].append((row, function))

    def unique(items):
        candidates = {(row['id'], fn['name'], fn['start']): (row, fn) for row, fn in items}
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    def resolve(row, caller, callee):
        normalized = callee.replace('::', '.')
        local = [fn for fn in row['analysis']['functions'] if fn['name'] == normalized]
        if not local and '.' in caller:
            relative = caller.rsplit('.', 1)[0] + '.' + normalized.removeprefix('self.').removeprefix('cls.')
            local = [fn for fn in row['analysis']['functions'] if fn['name'] == relative]
        result = unique((row, fn) for fn in local)
        if result:
            return result
        receiver, _, member = normalized.partition('.')
        receiver_types = {b['type'] for b in row['analysis'].get('receiver_bindings', []) if b['local'] == receiver}
        for binding in row['analysis'].get('import_bindings', []):
            name = binding.get('local')
            is_java_receiver = row['analysis'].get('language') == 'Java' and receiver_types == {name}
            if not name or receiver != name and not is_java_receiver:
                continue
            module = binding['module']
            if row['analysis'].get('language') in {'JavaScript', 'TypeScript'}:
                if not module.startswith('.'):
                    continue
                target_path = posixpath.normpath(posixpath.join(str(PurePosixPath(row['path']).parent), module))
                target_path = str(PurePosixPath(target_path).with_suffix('')) if PurePosixPath(target_path).suffix else target_path
                targets = by_path.get(target_path, []) + by_path.get(target_path + '/index', [])
            else:
                if module.startswith('.'):
                    depth = len(module) - len(module.lstrip('.'))
                    parts = list(PurePosixPath(row['path']).parent.parts)
                    module = '.'.join(parts[:len(parts)-depth+1] + [module.lstrip('.')]).rstrip('.')
                targets = modules.get(module, [])
            target_name = binding.get('symbol') or member
            if row['analysis'].get('language') == 'Python':
                if binding.get('symbol') and member:
                    submodule = module+'.'+binding['symbol']
                    if modules.get(submodule):
                        targets = modules[submodule]
                        target_name = member
                    else:
                        target_name = binding['symbol']+'.'+member
                elif not binding.get('symbol') and normalized.startswith(module+'.'):
                    target_name = normalized[len(module)+1:]
            if is_java_receiver or row['analysis'].get('language') == 'Java':
                target_name = name + '.' + member
            result = unique((target, fn) for target in targets for fn in target['analysis']['functions']
                            if fn['name'] == target_name or target_name == 'default' and fn.get('default_export'))
            if result:
                return result
        if row['analysis'].get('language') in {'C', 'C++'}:
            return unique(symbols[normalized])
        return None

    flows = []
    for row in code:
        for function in row['analysis']['functions']:
            for entry in function.get('entries', []):
                steps, edges, unknowns = [], [], []

                def append(current, fn, kind, item, title, parent, relation):
                    if len(steps) >= 80:
                        return None
                    identifier = digest([current['id'], fn['name'], kind, item, len(steps), version])[:24]
                    regions = []
                    if kind != 'function':
                        for branch in current['analysis'].get('branches', []):
                            if branch.get('function') != fn['name']:
                                continue
                            for arm in branch.get('arms', []):
                                if arm['start_line'] <= item['line'] <= arm['end_line']:
                                    regions.append({'branch_line': branch['line'], 'arm': arm['label'],
                                                    'condition': branch.get('condition'), 'runtime_verified': False})
                    steps.append({'step_id': identifier, 'order': len(steps)+1, 'kind': kind, 'title': title,
                                  'file_id': current['id'], 'details': {k: v for k, v in item.items()
                                    if k not in {'source_refs', 'query', 'entries'}},
                                  'source_refs': [source_ref(current, ref, version) for ref in item['source_refs']],
                                  'syntax_branch_regions': regions, 'runtime_verified': False})
                    if parent:
                        edges.append({'from': parent, 'to': identifier, 'relation': relation, 'runtime_verified': False})
                    return identifier

                def walk(current, fn, parent=None, depth=0, ancestors=frozenset()):
                    identity = (current['id'], fn['name'], fn['start'])
                    if identity in ancestors or depth > 8 or len(steps) >= 80:
                        unknowns.append('递归、超过8层或80步的路径未展开')
                        return
                    node = append(current, fn, 'function', fn, fn['name'], parent, 'static_call_target')
                    unknowns.extend(current['analysis'].get('unknowns', []))
                    if current['analysis'].get('parse_state') != 'complete':
                        unknowns.append(current['path']+' 解析不完整，路径可能遗漏')
                    events = []
                    for kind, key in [('branch', 'branches'), ('call', 'calls'), ('data_operation', 'data_operations')]:
                        for item in current['analysis'].get(key, []):
                            if item.get('caller' if kind == 'call' else 'function') == fn['name']:
                                events.append((item['line'], kind, item))
                    for _, kind, item in sorted(events, key=lambda event: (event[0], event[1])):
                        site = append(current, fn, kind, item, item.get('callee') or item.get('query') or item.get('kind'),
                                      node, 'contains')
                        if site is None:
                            unknowns.append('路径达到80步上限，后续语法事件未全部展开')
                            break
                        if kind == 'call':
                            resolved = resolve(current, fn['name'], item['callee'])
                            if resolved:
                                walk(*resolved, site, depth+1, ancestors | {identity})
                            else:
                                unknowns.append('调用 '+item['callee']+' 的分发或外部实现未知（'+current['path']+':'+str(item['line'])+'）')
                walk(row, function)
                role = 'startup' if 'startup' in entry else 'process' if entry == 'process_entry' else 'request'
                flows.append({'schema_version': 'business-flow-1',
                              'flow_id': digest([row['id'], function['name'], entry, version])[:24],
                              'profile_version': version, 'entry': entry, 'kind': role, 'steps': steps, 'edges': edges,
                              'source_refs': steps[0]['source_refs'], 'unknowns': list(dict.fromkeys(unknowns))[:30],
                              'verification_status': 'static', 'control_flow_verified': False,
                              'ordering': 'source positions and resolved call expansion; alternatives are syntax regions, not observed execution'})
                if len(flows) >= 100:
                    return flows
    return flows
