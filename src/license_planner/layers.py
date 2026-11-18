"""Offline audit of a mirror-style layered delivery.

The caller supplies only metadata: a strict policy, component expressions,
declared obligation material digests, and an ordered list of virtual layers.
No image, archive, symbolic link or file content is read; every path lives in
a virtual directory tree.

Layers are replayed in order.  Within one layer all ``whiteout``/``opaque``
markers are first evaluated against the tree as it stood *before* the layer,
then directory and regular-file writes are installed, then the layer's
hardlinks are resolved against the namespace produced by those normal writes.
Hardlinks bind the immutable ``_Object`` that the target regular file
denotes at that moment, so later overwrites or deletions of the target path
never re-point an existing binding.

Two equivalent modelling choices satisfy the same contract:

* materialise object identity per layer -- a ``file`` entry allocates one
  immutable object, and every later ``hardlink`` binding stores that object;
* keep immutable layer-versioned references -- a node stores
  ``(object_id, creation_layer)`` and hardlinks copy the pair.

This module uses the first.  The invariants are: an object's ``sha256`` and
``component`` never change; its ``origin`` is fixed at creation; a path node
is replaced wholesale (never mutated in place) when a file/hardlink write
lands on it; deleting a directory detaches its whole subtree but cannot
touch objects still bound elsewhere.  The second scheme would make origin
lookups explicit rather than implicit, at the cost of an extra indirection
on every read; both are correct because the contract only ever observes the
object bound to a path at layer end.  Resolution is iterative and memoised,
so a 10 000-step forward hardlink chain costs linear time and no recursion.
"""
import unicodedata

from .common import ident, require, rows
from .expression import alternatives, parse
from .model import license_outcome_choices, read_policy

LAYER_LIMIT = 256
OPERATION_LIMIT = 100000
COMPONENT_LIMIT = 10000
MATERIAL_LIMIT = 20000
PATH_LIMIT = 1024
SEGMENT_LIMIT = 128

_HEX = frozenset('0123456789abcdef')


class _Dir:
    __slots__ = ('children',)

    def __init__(self):
        self.children = {}


class _File:
    __slots__ = ('obj',)

    def __init__(self, obj):
        self.obj = obj


class _Object:
    __slots__ = ('sha', 'component', 'layer', 'path')

    def __init__(self, sha, component, layer, path):
        self.sha = sha
        self.component = component
        self.layer = layer
        self.path = path


def _path(value):
    """Validate one canonical relative POSIX path and return its segments."""
    if not isinstance(value, str) or not value:
        raise ValueError('expected nonempty relative path')
    if len(value) > PATH_LIMIT:
        raise ValueError('path exceeds 1024 characters')
    if '\\' in value:
        raise ValueError('path must not contain backslash')
    parts = tuple(value.split('/'))
    if len(parts) > SEGMENT_LIMIT:
        raise ValueError('path exceeds 128 segments')
    for part in parts:
        if part in ('', '.', '..'):
            raise ValueError('path must have no empty, dot or dotdot segment')
        if any(unicodedata.category(ch) == 'Cc' for ch in part):
            raise ValueError('path must not contain control characters')
    return parts


def _sha(value):
    if (not isinstance(value, str) or len(value) != 64
            or any(ch not in _HEX for ch in value)):
        raise ValueError('expected 64 lowercase hexadecimal sha256')
    return value


def _components(value):
    """Validate every component, including ones that stay invisible.

    The raw expression is kept; ``parse``/``alternatives`` run now so an
    invalid expression is fatal even when no final file exposes the
    component.  Alternatives are recomputed per exposed component only, so a
    large component catalogue that is mostly invisible never materialises all
    candidate sets at once.
    """
    result = {}
    for row in rows(value, COMPONENT_LIMIT):
        require(row, 'id expression')
        cid = ident(row['id'])
        if cid in result:
            raise ValueError('duplicate component')
        expression = row['expression']
        alternatives(parse(expression))
        result[cid] = expression
    return result


def _materials(value):
    declared = {}
    for row in rows(value, MATERIAL_LIMIT):
        require(row, 'obligation sha256')
        name = ident(row['obligation'])
        digest = _sha(row['sha256'])
        bucket = declared.get(name)
        if bucket is None:
            bucket = declared[name] = set()
        if digest in bucket:
            raise ValueError('duplicate obligation material digest')
        bucket.add(digest)
    return declared


def _layer_entries(entries, component_ids):
    """Validate and normalise one ordered layer.

    Returns ``(deletes, writes, links)`` where deletes are ``(kind, path,
    parts)``, writes are ``(kind, path, parts, payload)`` with payload
    ``(sha, component)`` for files, and links are ``(path, parts, target,
    target_parts)``.  A file/hardlink write may not be an ancestor of any
    other write path in the same layer; ``dir`` writes may.
    """
    writes = {}
    deletes = {}
    write_paths = []
    filelink_paths = set()
    for entry in entries:
        if not isinstance(entry, dict) or 'kind' not in entry:
            raise ValueError('entry must be an object with kind')
        kind = entry['kind']
        if kind == 'dir':
            require(entry, 'kind path')
            path = entry['path']
            parts = _path(path)
            payload = None
        elif kind == 'file':
            require(entry, 'kind path sha256 component')
            path = entry['path']
            parts = _path(path)
            digest = _sha(entry['sha256'])
            component = entry['component']
            if component is not None:
                if not isinstance(component, str) or component not in component_ids:
                    raise ValueError('file component must be null or an existing id')
            payload = (digest, component)
        elif kind == 'hardlink':
            require(entry, 'kind path target')
            path = entry['path']
            parts = _path(path)
            target = entry['target']
            target_parts = _path(target)
            payload = (target, target_parts)
        elif kind in ('whiteout', 'opaque'):
            require(entry, 'kind path')
            path = entry['path']
            parts = _path(path)
            payload = None
        else:
            raise ValueError('unknown entry kind')
        if kind in ('dir', 'file', 'hardlink'):
            if path in writes:
                raise ValueError('duplicate write path in layer')
            writes[path] = (kind, path, parts, payload)
            write_paths.append(path)
            if kind != 'dir':
                filelink_paths.add(path)
        else:
            if path in deletes:
                raise ValueError('duplicate delete path in layer')
            deletes[path] = (kind, path, parts)
    for path in write_paths:
        parts = writes[path][2]
        for index in range(1, len(parts)):
            if '/'.join(parts[:index]) in filelink_paths:
                raise ValueError('file or hardlink write cannot be an ancestor')
    deletes_list = [deletes[p] for p in sorted(deletes)]
    writes_list = [writes[p] for p in write_paths]
    links = [(w[1], w[2], w[3][0], w[3][1])
             for w in writes_list if w[0] == 'hardlink']
    normal = [w for w in writes_list if w[0] != 'hardlink']
    normal.sort(key=lambda w: (len(w[2]), w[1]))
    links.sort(key=lambda w: (len(w[2]), w[1]))
    return deletes_list, normal, links


def _layers(value, component_ids):
    layers = []
    seen = set()
    total = 0
    for row in rows(value, LAYER_LIMIT):
        require(row, 'id entries')
        layer_id = ident(row['id'])
        if layer_id in seen:
            raise ValueError('duplicate layer id')
        seen.add(layer_id)
        entries = row['entries']
        if not isinstance(entries, list):
            raise ValueError('entries must be an array')
        total += len(entries)
        if total > OPERATION_LIMIT:
            raise ValueError('too many entries')
        layers.append((layer_id, _layer_entries(entries, component_ids)))
    return layers


def _lookup(root, parts):
    node = root
    for part in parts:
        if not isinstance(node, _Dir):
            return None
        node = node.children.get(part)
        if node is None:
            return None
    return node


def _remove(root, parts):
    parent = _lookup(root, parts[:-1])
    if isinstance(parent, _Dir):
        parent.children.pop(parts[-1], None)


def _parent(root, parts):
    node = root
    for part in parts[:-1]:
        child = node.children.get(part)
        if child is None:
            child = node.children[part] = _Dir()
        elif isinstance(child, _File):
            # The only legal way to pass an inherited file is an explicit dir
            # write for that exact path, which sorts earlier by depth.
            raise ValueError('write path has a regular-file parent')
        node = child
    return node


def _declare_deletes(root, deletes):
    """Check opaque legality against the pre-layer tree (no mutation)."""
    for kind, _path_text, parts in deletes:
        if kind == 'opaque':
            node = _lookup(root, parts)
            if isinstance(node, _File):
                raise ValueError('opaque target was a regular file before layer')


def _apply_delete(root, kind, parts):
    if kind == 'whiteout':
        _remove(root, parts)
        return
    node = _lookup(root, parts)
    if isinstance(node, _Dir):
        node.children.clear()


def _apply_deletes(root, deletes):
    """Apply the union of pre-layer deletion sets.

    Legality was already checked against the pre-layer tree.  Every marker
    only removes nodes, and removal is idempotent, so applying markers one by
    one yields exactly the union of their pre-layer deletion sets regardless
    of order; a marker whose target an earlier marker already detached is a
    no-op, which is the same set again.
    """
    for kind, _path_text, parts in deletes:
        _apply_delete(root, kind, parts)


def _install_normal(root, writes, layer_id):
    for kind, path_text, parts, payload in writes:
        parent = _parent(root, parts)
        name = parts[-1]
        if kind == 'dir':
            if not isinstance(parent.children.get(name), _Dir):
                # Absent path, or an inherited regular file being replaced by
                # this layer's explicit directory write.
                parent.children[name] = _Dir()
        else:
            digest, component = payload
            parent.children[name] = _File(
                _Object(digest, component, layer_id, path_text))


def _resolve_links(root, links):
    """Resolve every link target against the post-normal-write namespace.

    A target that names another hardlink of the same layer follows that
    definition (forward references allowed); anything else is read from the
    ordinary tree.  The old bindings at this layer's link paths are detached
    first, so an inherited subtree under a link path is never mistaken for
    the freshly bound object.  Cycles and self-loops are rejected.  The walk
    is an explicit stack with memoisation, so chain depth is irrelevant.
    """
    for _path_text, parts, _target, _target_parts in links:
        _remove(root, parts)
    targets = {path: (target, target_parts)
               for path, _parts, target, target_parts in links}
    resolved = {}
    state = {}
    for path, _parts, _target, _target_parts in links:
        if state.get(path) == 2:
            continue
        chain = []
        cursor = path
        while True:
            status = state.get(cursor, 0)
            if status == 2:
                obj = resolved[cursor]
                break
            if status == 1:
                raise ValueError('hardlink cycle')
            state[cursor] = 1
            chain.append(cursor)
            target, target_parts = targets[cursor]
            if target in targets:
                cursor = target
                continue
            node = _lookup(root, target_parts)
            if node is None:
                raise ValueError('hardlink target does not exist')
            if isinstance(node, _Dir):
                raise ValueError('hardlink target is a directory')
            obj = node.obj
            break
        for step in reversed(chain):
            resolved[step] = obj
            state[step] = 2
    return resolved


def _install_links(root, links, resolved):
    for _path_text, parts, _target, _target_parts in links:
        parent = _parent(root, parts)
        parent.children[parts[-1]] = _File(resolved[_path_text])


def _visible_files(root):
    found = []
    stack = [(root, '')]
    while stack:
        node, prefix = stack.pop()
        for name, child in node.children.items():
            path = name if not prefix else prefix + '/' + name
            if isinstance(child, _File):
                found.append((path, child.obj))
            else:
                stack.append((child, path))
    return found


def audit(data):
    """Validate one layered delivery input and return its business result."""
    require(data, 'policy components materials layers')
    policy = read_policy(data['policy'])
    component_expressions = _components(data['components'])
    declared = _materials(data['materials'])
    layers = _layers(data['layers'], component_expressions)

    root = _Dir()
    for layer_id, (deletes, writes, links) in layers:
        _declare_deletes(root, deletes)
        _apply_deletes(root, deletes)
        _install_normal(root, writes, layer_id)
        resolved = _resolve_links(root, links)
        _install_links(root, links, resolved)

    visible = _visible_files(root)
    visible.sort(key=lambda item: item[0])

    files = []
    exposed = {}
    by_digest = {}
    for path, obj in visible:
        files.append({
            'path': path,
            'sha256': obj.sha,
            'component': obj.component,
            'origin': {'layer': obj.layer, 'path': obj.path},
        })
        if obj.component is not None:
            exposed.setdefault(obj.component, []).append(path)
        by_digest.setdefault(obj.sha, []).append(path)

    components = []
    required = {}
    for cid in sorted(exposed):
        status, selected, _permitted, _rejected = license_outcome_choices(
            policy, alternatives(parse(component_expressions[cid])))
        components.append({
            'id': cid,
            'status': status,
            'selected': selected,
            'paths': sorted(exposed[cid]),
        })
        if selected is not None:
            for obligation in selected['obligations']:
                required.setdefault(obligation, set()).add(cid)

    obligations = []
    for obligation in sorted(required):
        digests = declared.get(obligation, set())
        paths = sorted(
            path
            for digest in digests
            for path in by_digest.get(digest, ()))
        obligations.append({
            'obligation': obligation,
            'components': sorted(required[obligation]),
            'satisfied': bool(paths),
            'paths': paths,
        })

    compliant = (all(row['status'] == 'allowed' for row in components)
                 and all(row['satisfied'] for row in obligations))
    return {
        'compliant': compliant,
        'files': files,
        'components': components,
        'obligations': obligations,
    }
