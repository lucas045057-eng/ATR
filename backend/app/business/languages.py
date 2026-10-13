"""Bounded syntax inspection. Source is data; it is never imported or executed."""
from __future__ import annotations

import ast
import hashlib
import importlib
import re
import warnings
from pathlib import Path

from app.tools.redaction import redact_text

LANGUAGES = {'.py': 'Python', '.js': 'JavaScript', '.jsx': 'JavaScript', '.mjs': 'JavaScript',
             '.cjs': 'JavaScript', '.ts': 'TypeScript', '.tsx': 'TypeScript', '.java': 'Java',
             '.c': 'C', '.h': 'C', '.cc': 'C++', '.cpp': 'C++', '.cxx': 'C++', '.hpp': 'C++', '.hxx': 'C++'}
GRAMMARS = {'JavaScript': ('tree_sitter_javascript', 'language'),
            'TypeScript': ('tree_sitter_typescript', 'language_typescript'),
            'Java': ('tree_sitter_java', 'language'), 'C': ('tree_sitter_c', 'language'),
            'C++': ('tree_sitter_cpp', 'language')}
HTTP = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}
VERSION = 'ab-source-syntax-2'


def sql_operation(text, ref):
    stripped = text.strip()
    match = re.match(r'(?is)^(SELECT|INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b', stripped)
    if not match:
        return None
    keyword = match[1].upper()
    sensitive=bool(re.search(r'(?i)\b(?:password|passwd|api_key|secret|access_token|refresh_token|private_key)\b',stripped))
    tables = re.findall(r'(?i)\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+["`]?([A-Za-z_][A-Za-z0-9_.]*)', stripped)
    return {'operation': 'read' if keyword == 'SELECT' else 'write', 'sql_verb': keyword,
            'tables': list(dict.fromkeys(tables))[:8], 'probe': bool(re.fullmatch(r'(?is)SELECT\s+1\s*;?', stripped)),
            'query': keyword+' [REDACTED_SENSITIVE_SQL]' if sensitive else redact_text(stripped)[:600],
            'sensitive':sensitive, 'source_refs': [ref]}


def describe_language(path: Path | str, content: str) -> dict:
    relative = str(path).replace('\\', '/')
    suffix = Path(relative).suffix.lower()
    language = LANGUAGES.get(suffix)
    info = {'language': language or '', 'parser_version': VERSION, 'parse_state': 'complete',
            'imports': [], 'import_bindings': [], 'routes': [], 'symbols': [], 'functions': [],
            'calls': [], 'branches': [], 'data_operations': [], 'environment_keys': [],
            'client_calls': [], 'receiver_bindings': [], 'unknowns': []}
    raw = content.encode('utf-8')
    if language is None or len(raw) > 256*1024:
        info['parse_state'] = 'unsupported_language' if language is None else 'read_limit'
        return info
    digest = hashlib.sha256(raw).hexdigest()

    def ref(start, end=None):
        return {'schema_version': 'source-ref-1', 'path': relative, 'content_digest': digest,
                'digest_basis': 'decoded_utf8', 'start_line': start, 'end_line': end or start,
                'source_type': 'Static', 'parser_version': VERSION}

    try:
        if language == 'Python':
            _python(content, relative, info, ref)
        else:
            _tree(raw, language, suffix, info, ref)
    except (SyntaxError, ValueError, RecursionError):
        info['parse_state'] = 'partial_parser_error'
        info['unknowns'].append('语法不完整；只保留已定位的局部结构，不推断动态行为')
    except ImportError:
        info['parse_state'] = 'parser_unavailable'
        info['unknowns'].append('此语言解析器不可用，未用正则替代语法理解')
    for key, limit in [('symbols', 300), ('functions', 300), ('routes', 100), ('calls', 1000),
                       ('branches', 300), ('data_operations', 300), ('import_bindings', 200)]:
        if len(info[key]) > limit:
            info[key] = info[key][:limit]
            info['parse_state'] = 'partial_parser_error'
            info['unknowns'].append(key + ' 达到解析容量上限')
    info['imports'] = sorted(set(info['imports']))[:200]
    info['environment_keys'] = sorted(set(info['environment_keys']))[:100]
    return info


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return prefix + '.' + node.attr if prefix else None
    return None


def _python(content, relative, info, ref):
    tree = ast.parse(content, filename=relative)
    items = list(ast.walk(tree))
    if len(items) > 100000:
        raise ValueError('Python syntax node limit')
    parents = {child: node for node in items for child in ast.iter_child_nodes(node)}
    def qualified(node):
        names = [node.name]
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name)
        return '.'.join(reversed(names))
    functions = {node: qualified(node) for node in items if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    startup_names = set()
    constructors = {'fastapi.FastAPI','fastapi.APIRouter','flask.Flask','flask.Blueprint'}
    routers = {}
    for node in items:
        if isinstance(node, ast.Import):
            info['imports'] += [alias.name for alias in node.names]
            info['import_bindings'] += [{'local': alias.asname or alias.name.split('.')[0], 'module': alias.name,
                                        'symbol': None, 'source_refs': [ref(node.lineno)]} for alias in node.names]
            for alias in node.names:
                if alias.name in {'fastapi', 'flask'}:
                    prefix = alias.asname or alias.name
                    constructors.update(prefix+'.'+name for name in
                                        ('FastAPI','APIRouter','Flask','Blueprint'))
        elif isinstance(node, ast.ImportFrom):
            module = '.'*node.level + (node.module or '')
            info['imports'].append(module)
            info['import_bindings'] += [{'local': alias.asname or alias.name, 'module': module, 'symbol': alias.name,
                                        'source_refs': [ref(node.lineno)]} for alias in node.names]
            if module in {'fastapi','flask'}:
                constructors.update(alias.asname or alias.name for alias in node.names
                                    if alias.name in {'FastAPI','APIRouter','Flask','Blueprint'})
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == 'lifespan' and isinstance(keyword.value, ast.Name):
                    startup_names.add(keyword.value.id)
    for node in items:
        if isinstance(node,(ast.Assign,ast.AnnAssign)) and isinstance(node.value,ast.Call) and _dotted(node.value.func) in constructors:
            targets = node.targets if isinstance(node,ast.Assign) else [node.target]
            prefix = next((kw.value.value for kw in node.value.keywords if kw.arg in {'prefix','url_prefix'}
                           and isinstance(kw.value,ast.Constant) and isinstance(kw.value.value,str)), '')
            for target in targets:
                if isinstance(target,ast.Name):
                    routers[target.id] = prefix

    def owner(node):
        while node in parents:
            node = parents[node]
            if node in functions:
                return functions[node]
        return None

    for node in items:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            name = qualified(node)
            info['symbols'].append({'name': name, 'short_name': node.name, 'start': start, 'end': node.end_lineno})
            if isinstance(node, ast.ClassDef):
                continue
            entries = []
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                route_method = decorator.func.attr
                literal = decorator.args[0].value if decorator.args and isinstance(decorator.args[0], ast.Constant) else None
                if route_method == 'on_event' and literal == 'startup':
                    entries.append('startup')
                if route_method in HTTP | {'route'} and isinstance(literal, str):
                    receiver = _dotted(decorator.func.value)
                    if receiver not in routers:
                        info['unknowns'].append('未解析请求宿主位于第 '+str(decorator.lineno)+' 行，未推断 HTTP 路由')
                        if receiver not in {'app','router'} or not {'fastapi','flask'}.intersection(info['imports']):
                            continue
                    literal = routers.get(receiver, '').rstrip('/') + '/' + literal.lstrip('/')
                    methods = [route_method.upper()] if route_method != 'route' else ['GET']
                    for keyword in decorator.keywords:
                        if keyword.arg == 'methods' and isinstance(keyword.value, (ast.List, ast.Tuple)):
                            methods = [value.value.upper() for value in keyword.value.elts
                                       if isinstance(value, ast.Constant) and isinstance(value.value, str)
                                       and value.value.lower() in HTTP]
                    for method in methods:
                        info['routes'].append({'method': method, 'path': redact_text(literal)[:240], 'symbol': name,
                                              'line': decorator.lineno, 'source_refs': [ref(decorator.lineno)],
                                              'receiver_status':'bound' if receiver in routers else 'unverified_receiver'})
                        entries.append(method + ' ' + redact_text(literal)[:240])
            if node.name in startup_names:
                entries.append('lifespan_startup_and_shutdown')
            info['functions'].append({'name': name, 'qualified_name': name, 'start': start,
                                      'end': node.end_lineno, 'entries': entries,
                                      'source_refs': [ref(start, node.end_lineno)]})
        if isinstance(node, ast.Call):
            callee = _dotted(node.func)
            source = ref(node.lineno, node.end_lineno)
            caller = owner(node)
            if callee:
                info['calls'].append({'caller': caller, 'callee': callee, 'line': node.lineno,
                                      'dispatch': 'static_syntax', 'source_refs': [source]})
                client, _, method = callee.rpartition('.')
                if client in {'pymysql', 'psycopg', 'psycopg2', 'redis'} and method in {'connect', 'Redis', 'from_url'}:
                    info['client_calls'].append({'client': client, 'method': method, 'line': node.lineno})
                if callee in {'os.getenv', 'os.environ.get'} and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    info['environment_keys'].append(node.args[0].value[:120])
                for keyword in node.keywords:
                    if keyword.arg == 'status_code' and isinstance(keyword.value, ast.Constant) and type(keyword.value.value) is int:
                        info['branches'].append({'function': caller, 'kind': 'http_status', 'status_code': keyword.value.value,
                                                 'line': node.lineno, 'source_refs': [source]})
            else:
                info['unknowns'].append('动态调用位于第 '+str(node.lineno)+' 行，未推断实际目标')
            for argument in node.args if callee and callee.rsplit('.',1)[-1] in {'execute','executemany','executescript','query','prepare','raw'} else []:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    operation = sql_operation(argument.value, source)
                    if operation:
                        parameters=[]
                        for other in node.args:
                            if other is argument:
                                continue
                            values=other.elts if isinstance(other,(ast.Tuple,ast.List)) else [other]
                            parameters += [redact_text(str(value.value))[:120] for value in values
                                           if isinstance(value,ast.Constant) and isinstance(value.value,(str,int,float,bool))]
                        info['data_operations'].append({**operation, 'function': caller, 'line': node.lineno,
                                                        'literal_parameters':['[REDACTED_PARAMETERS]'] if operation['sensitive'] else parameters[:20]})
        elif isinstance(node, (ast.If, ast.IfExp, ast.Try, ast.Match)):
            arms = []
            groups = [('true', getattr(node,'body',[])), ('false',getattr(node,'orelse',[])),
                      ('finally',getattr(node,'finalbody',[]))]
            groups += [('except:'+ast.unparse(handler.type) if handler.type else 'except', handler.body)
                       for handler in getattr(node,'handlers',[])]
            groups += [('case:'+redact_text(ast.unparse(case.pattern))[:120],case.body)
                       for case in getattr(node,'cases',[])]
            for label, body in groups:
                if not isinstance(body,list):
                    body=[body]
                if body:
                    arms.append({'label':label,'start_line':body[0].lineno,'end_line':body[-1].end_lineno})
            info['branches'].append({'function': owner(node), 'kind': type(node).__name__, 'line': node.lineno,
                                      'end_line': node.end_lineno, 'arms':arms,
                                      'condition':redact_text(ast.unparse(node.test))[:200] if hasattr(node,'test') else None,
                                      'source_refs': [ref(node.lineno, node.end_lineno)]})
        elif isinstance(node, ast.Subscript) and _dotted(node.value) == 'os.environ' and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            info['environment_keys'].append(node.slice.value[:120])


def _tree(raw, language, suffix, info, ref):
    from tree_sitter import Language, Parser
    module_name, factory = GRAMMARS[language]
    if suffix == '.tsx':
        factory = 'language_tsx'
    parser = Parser(Language(getattr(importlib.import_module(module_name), factory)()))
    # shortcut: 0.25.2 callback crashes on Python 3.12; upgrade only after both platform gates pass.
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Use the progress_callback in parse\\(\\)', category=DeprecationWarning)
        parser.timeout_micros = 1000000
    tree = parser.parse(raw)
    if tree is None:
        raise ValueError('Grammar parse timeout')
    if tree.root_node.has_error:
        info['parse_state'] = 'partial_parser_error'
        info['unknowns'].append('存在语法错误节点；已解析的语法不证明编译或运行成功')

    def text(node):
        return raw[node.start_byte:node.end_byte].decode('utf-8') if node else ''

    def literal(node):
        if node is None or node.type not in {'string', 'string_literal', 'template_string'}:
            return None
        value = text(node)
        if node.type == 'template_string' and '${' in value:
            return None
        return value[1:-1] if value[:1] in {'"', "'", '`'} and value[-1:] == value[:1] else None

    def dotted(node):
        if node is None:
            return None
        if node.type in {'identifier', 'property_identifier', 'field_identifier', 'type_identifier', 'qualified_identifier', 'namespace_identifier'}:
            value = text(node)
            return value if re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*(?:(?:\.|::)[A-Za-z_$][A-Za-z0-9_$]*)*', value) else None
        if node.type in {'member_expression', 'field_expression'}:
            prefix = dotted(node.child_by_field_name('object') or node.child_by_field_name('argument'))
            prop = dotted(node.child_by_field_name('property') or node.child_by_field_name('field'))
            return prefix+'.'+prop if prefix and prop else None
        return None

    def imports(node):
        source = ref(node.start_point.row+1, node.end_point.row+1)
        if node.type == 'import_statement':
            module = literal(node.child_by_field_name('source'))
            if module is None:
                return
            info['imports'].append(module)
            clause=next((c for c in node.named_children if c.type=='import_clause'),None)
            for child in clause.named_children if clause else []:
                if child.type=='identifier':
                    info['import_bindings'].append({'local':text(child),'module':module,'symbol':'default','source_refs':[source]})
                elif child.type=='namespace_import':
                    local=next((text(c) for c in child.named_children if c.type=='identifier'),None)
                    info['import_bindings'].append({'local':local,'module':module,'symbol':None,'source_refs':[source]})
                elif child.type=='named_imports':
                    for item in child.named_children:
                        named=item.child_by_field_name('name')
                        alias=item.child_by_field_name('alias') or named
                        if named:
                            info['import_bindings'].append({'local':text(alias),'module':module,'symbol':text(named),'source_refs':[source]})
        elif node.type=='import_declaration':
            module=text(node).removeprefix('import ').removeprefix('static ').rstrip(';').strip()
            info['imports'].append(module)
            info['import_bindings'].append({'local':module.rsplit('.',1)[-1],'module':module,'symbol':None,'source_refs':[source]})
        elif node.type=='preproc_include':
            value=text(node.child_by_field_name('path')).strip('<>"')
            if value:
                info['imports'].append(value)

    def annotation_paths(annotation):
        args=annotation.child_by_field_name('arguments')
        paths=[]
        for child in args.named_children if args else []:
            if child.type=='element_value_pair':
                if text(child.child_by_field_name('key')) not in {'path','value'}:
                    continue
                child=child.child_by_field_name('value')
            values=child.named_children if child.type=='element_value_array_initializer' else [child]
            paths.extend(value for value in map(literal,values) if value is not None)
        return paths[:10]

    function_types = {'function_declaration', 'function_definition', 'method_definition', 'method_declaration', 'arrow_function', 'function_expression'}
    routers = set()
    # Receiver identity is a static binding, not a method-name heuristic.
    pre_scan = [tree.root_node]
    while pre_scan:
        node = pre_scan.pop()
        imports(node)
        if node.type=='package_declaration':
            info['package']=text(node).removeprefix('package ').rstrip(';').strip()
        if node.type in {'field_declaration','local_variable_declaration','formal_parameter'} and language=='Java':
            typ=text(node.child_by_field_name('type'))
            for child in node.named_children:
                name=child.child_by_field_name('name') if child.type=='variable_declarator' else node.child_by_field_name('name')
                if name and typ:
                    info['receiver_bindings'].append({'local':text(name),'type':typ,'source_refs':[ref(node.start_point.row+1)]})
        if node.type == 'variable_declarator':
            value = node.child_by_field_name('value')
            if value and value.type == 'call_expression':
                target = dotted(value.child_by_field_name('function'))
                bindings=info['import_bindings']
                express_names={b['local'] for b in bindings if b['module']=='express' and b['symbol'] in {'default',None}}
                router_names={b['local'] for b in bindings if b['module']=='express' and b['symbol']=='Router'}
                if target in express_names | router_names | {name+'.Router' for name in express_names}:
                    name = dotted(node.child_by_field_name('name'))
                    if name:
                        routers.add(name)
                args=value.child_by_field_name('arguments')
                first=args.named_children[0] if args and args.named_children else None
                module=literal(first)
                if target=='require' and module:
                    local=dotted(node.child_by_field_name('name'))
                    if local:
                        info['imports'].append(module)
                        info['import_bindings'].append({'local':local,'module':module,'symbol':None,'source_refs':[ref(node.start_point.row+1)]})
                        if module=='express':
                            # require binding is traversed before later constructor declarations.
                            express_names.add(local)
        pre_scan.extend(reversed(node.named_children))
    node_count = 0
    stack = [(tree.root_node, None)]
    while stack:
        node, caller = stack.pop()
        node_count += 1
        if node_count > 100000:
            raise ValueError('Grammar syntax node limit')
        line, end = node.start_point.row+1, node.end_point.row+1
        source = ref(line, end)
        if node.type in function_types:
            name = dotted(node.child_by_field_name('name'))
            declarator = node.child_by_field_name('declarator')
            while declarator and not name:
                name = dotted(declarator.child_by_field_name('declarator'))
                declarator = declarator.child_by_field_name('declarator')
            if not name and node.parent and node.parent.type == 'variable_declarator':
                name = dotted(node.parent.child_by_field_name('name'))
            if not name:
                name = '@callback:'+str(line)+':'+str(node.start_point.column)
            parent = node.parent
            prefix = []
            while parent:
                if parent.type in {'class_declaration', 'class_definition', 'namespace_definition'}:
                    class_name = dotted(parent.child_by_field_name('name'))
                    if class_name:
                        prefix.insert(0,class_name)
                parent = parent.parent
            if prefix:
                name = '.'.join([*prefix,name])
            caller = name
            info['symbols'].append({'name': name, 'start': line, 'end': end})
            info['functions'].append({'name': name, 'qualified_name': name, 'start': line, 'end': end,
                                      'entries': [], 'source_refs': [source],
                                      'default_export': bool(node.parent and node.parent.type=='export_statement'
                                                             and any(c.type=='default' for c in node.parent.children))})
            if language == 'Java':
                modifiers = next((child for child in node.named_children if child.type == 'modifiers'), None)
                for annotation in modifiers.named_children if modifiers else []:
                    if annotation.type not in {'annotation', 'marker_annotation'}:
                        continue
                    mapping = text(annotation.child_by_field_name('name'))
                    if mapping == 'PostConstruct':
                        info['functions'][-1]['entries'].append('startup')
                    if mapping not in {'GetMapping','PostMapping','PutMapping','DeleteMapping','PatchMapping','RequestMapping'}:
                        continue
                    args = annotation.child_by_field_name('arguments')
                    paths = annotation_paths(annotation)
                    if not paths:
                        info['unknowns'].append('动态或复合 Java 请求映射位于第 '+str(line)+' 行')
                        continue
                    prefixes=['']
                    parent=node.parent
                    while parent:
                        if parent.type=='class_declaration':
                            mods=next((c for c in parent.named_children if c.type=='modifiers'),None)
                            prefix_annotation=next((c for c in mods.named_children
                                                    if c.type=='annotation' and text(c.child_by_field_name('name'))=='RequestMapping'),None) if mods else None
                            if prefix_annotation:
                                prefixes=annotation_paths(prefix_annotation)
                                if not prefixes:
                                    info['unknowns'].append('类的动态 Java 请求前缀尚未解析')
                        parent=parent.parent
                    method = mapping.removesuffix('Mapping').upper() if mapping != 'RequestMapping' else 'UNSPECIFIED'
                    for prefix in prefixes:
                        for path in paths:
                            full=prefix.rstrip('/')+'/'+path.lstrip('/')
                            info['routes'].append({'method': method, 'path': redact_text(full)[:240], 'symbol': name,
                                                   'line': annotation.start_point.row+1, 'source_refs': [ref(annotation.start_point.row+1)]})
                            info['functions'][-1]['entries'].append(method+' '+full)
        if node.type in {'call_expression','method_invocation'}:
            callee_node = node.child_by_field_name('function') or node.child_by_field_name('name')
            callee = dotted(callee_node)
            if node.type == 'method_invocation' and node.child_by_field_name('object'):
                obj = dotted(node.child_by_field_name('object'))
                callee = obj+'.'+callee if obj and callee else None
            args = node.child_by_field_name('arguments')
            arguments = list(args.named_children) if args else []
            if callee:
                info['calls'].append({'caller': caller, 'callee': callee, 'line': line,
                                      'dispatch': 'static_syntax', 'source_refs': [source]})
                method = callee.rsplit('.',1)[-1].lower()
                route_path = literal(arguments[0]) if arguments else None
                if (language in {'JavaScript','TypeScript'} and method in HTTP and route_path is not None
                        and callee.rsplit('.',1)[0] in routers):
                    callback = next((a for a in arguments[1:] if a.type in function_types), None)
                    name = '@callback:'+str(callback.start_point.row+1)+':'+str(callback.start_point.column) if callback else dotted(arguments[1]) if len(arguments)>1 else None
                    if name:
                        info['routes'].append({'method': method.upper(), 'path': redact_text(route_path)[:240],
                                               'symbol': name, 'line': line, 'source_refs': [source]})
                if callee in {'getenv','System.getenv'} and route_path:
                    info['environment_keys'].append(route_path[:120])
                if method in {'status','senderror'} and arguments and arguments[0].type in {'number','decimal_integer_literal'}:
                    value = text(arguments[0])
                    if value.isdecimal():
                        info['branches'].append({'function': caller, 'kind':'http_status','status_code':int(value),
                                                 'line':line,'source_refs':[source]})
            else:
                info['unknowns'].append('动态调用位于第 '+str(line)+' 行，未推断实际目标')
            for argument in arguments if callee and callee.rsplit('.',1)[-1] in {'execute','query','prepare','raw','exec','executemany'} else []:
                value = literal(argument)
                operation = sql_operation(value, source) if value else None
                if operation:
                    parameters=[]
                    for other in arguments:
                        if other==argument:
                            continue
                        values=other.named_children if other.type in {'array','array_initializer'} else [other]
                        parameters += [redact_text(v)[:120] for child in values if (v:=literal(child)) is not None]
                    info['data_operations'].append({**operation,'function':caller,'line':line,
                                                   'literal_parameters':['[REDACTED_PARAMETERS]'] if operation['sensitive'] else parameters[:20]})
        if node.type in {'if_statement','conditional_expression','try_statement','switch_statement'}:
            arms=[]
            for label, field in [('true','consequence'),('false','alternative'),('try','body')]:
                child=node.child_by_field_name(field)
                if child:
                    arms.append({'label':label,'start_line':child.start_point.row+1,'end_line':child.end_point.row+1})
            for child in node.named_children:
                if child.type in {'catch_clause','finally_clause'}:
                    arms.append({'label':child.type,'start_line':child.start_point.row+1,'end_line':child.end_point.row+1})
            condition=node.child_by_field_name('condition')
            info['branches'].append({'function':caller,'kind':node.type,'line':line,'end_line':end,
                                    'arms':arms,'condition':redact_text(text(condition))[:200] if condition else None,'source_refs':[source]})
        if node.type == 'member_expression':
            value = dotted(node)
            if value and re.fullmatch(r'process\.env\.[A-Z0-9_]+',value):
                info['environment_keys'].append(value.rsplit('.',1)[-1])
        stack.extend((child,caller) for child in reversed(node.named_children))
    for function in info['functions']:
        if function['name'] == 'main':
            function['entries'].append('process_entry')
    routes = {route['symbol']: [] for route in info['routes']}
    for route in info['routes']:
        routes[route['symbol']].append(route['method']+' '+route['path'])
    for function in info['functions']:
        function['entries'] = list(dict.fromkeys(function['entries']+routes.get(function['name'],[])))
