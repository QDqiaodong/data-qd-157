"""Single-point-of-failure audit over a multi-root supply graph.

The audit works on one immutable graph and one reachable scope derived from
it; nodes are never removed up front because a component is blocked.

Definitions used here (all paths are directed paths in the supplied graph):

* The *scope* is the set of components reachable from any root, roots
  themselves included.
* Deactivating a scope component ``x`` means deleting ``x`` and its
  incident edges while every other root keeps supplying independently.
  ``lost_count(x)`` is the number of original scope components that are no
  longer reachable afterwards, ``x`` itself included.
* A *strict dominator* of ``x`` is a component ``d != x`` lying on every
  path from any root to ``x``.  The *immediate dominator* is the unique
  strict dominator closest to ``x``; roots (and any node with several
  independent root paths) have ``None``.
* ``blocked_dominator_count(x)`` counts blocked components that dominate
  ``x``, including ``x`` itself when it is blocked; the nearest such
  component is reported separately.  Blocking never deletes the node or an
  edge: the count is evidence about mandatory passage, not a sum over
  paths and not a licence-compliance verdict for any path.

Self loops and directed cycles are legal inputs.  Dominators are computed
with the iterative Cooper-Harvey-Kennedy algorithm on an explicit virtual
entry node, which is exact in the presence of cycles, self loops and
multiple roots.  Deactivating ``x`` disconnects exactly the nodes
dominated by ``x``, so ``lost_count`` is the size of ``x``'s subtree in
the dominator tree -- one immutable graph, no repeated per-node graph
mutations.  All traversals are iterative, so deep chains never rely on
the Python recursion limit.
"""

COMPONENT_LIMIT = 30000
EDGE_LIMIT = 120000


def validate_graph(graph, known):
    """Validate the optional dependency_graph value.

    ``known`` is the set of exact component ids already accepted by the
    licence analysis, so every graph reference is guaranteed to exist.
    Returns the deduplicated ``(roots, edges)`` pair; duplicate roots or
    duplicate directed edges are rejected while self loops and cycles are
    permitted.
    """
    if not isinstance(graph, dict) or set(graph) != {'roots', 'edges'}:
        raise ValueError('dependency_graph must contain exactly roots and edges')
    raw_roots = graph['roots']
    if not isinstance(raw_roots, list) or len(raw_roots) > COMPONENT_LIMIT:
        raise ValueError('roots must be a bounded array')
    roots = []
    seen_roots = set()
    for value in raw_roots:
        root = value if isinstance(value, str) else None
        if root is None or root not in known:
            raise ValueError('dependency root must reference an existing component id')
        if root in seen_roots:
            raise ValueError('duplicate dependency root')
        seen_roots.add(root)
        roots.append(root)
    raw_edges = graph['edges']
    if not isinstance(raw_edges, list) or len(raw_edges) > EDGE_LIMIT:
        raise ValueError('edges must be a bounded array')
    edges = []
    seen_edges = set()
    for entry in raw_edges:
        if not isinstance(entry, dict) or set(entry) != {'dependency', 'dependent'}:
            raise ValueError('edge must contain exactly dependency and dependent')
        dependency, dependent = entry['dependency'], entry['dependent']
        if not isinstance(dependency, str) or not isinstance(dependent, str):
            raise ValueError('edge endpoints must be component id strings')
        if dependency not in known or dependent not in known:
            raise ValueError('edge endpoint must reference an existing component id')
        pair = (dependency, dependent)
        if pair in seen_edges:
            raise ValueError('duplicate directed dependency edge')
        seen_edges.add(pair)
        edges.append(pair)
    return roots, edges


def _reachable(adjacency, starts):
    """Iterative forward reachability from the given start nodes."""
    seen = set(starts)
    stack = list(starts)
    while stack:
        node = stack.pop()
        for nxt in adjacency.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _reverse_postorder(adjacency, entry, scope):
    """Iterative DFS postorder restricted to ``scope``; returns RPO ids."""
    order = []
    visited = {entry}
    stack = [(entry, 0)]
    while stack:
        node, index = stack[-1]
        neighbours = adjacency.get(node, ())
        if index < len(neighbours):
            nxt = neighbours[index]
            stack[-1] = (node, index + 1)
            if nxt in scope and nxt not in visited:
                visited.add(nxt)
                stack.append((nxt, 0))
        else:
            order.append(node)
            stack.pop()
    order.reverse()
    return order


def _intersect(idom, finger1, finger2):
    """Cooper-Harvey-Kennedy intersect over postorder-numbered nodes."""
    while finger1 != finger2:
        while finger1 > finger2:
            finger1 = idom[finger1]
        while finger2 > finger1:
            finger2 = idom[finger2]
    return finger1


def _immediate_dominators(roots, edges, scope):
    """Immediate dominators for every scope node.

    A virtual entry (``None``) supplies every root, so a node reachable
    from two independent roots has no real-component dominator.  The
    virtual entry never appears in the returned mapping.
    """
    adjacency = {}
    predecessors = {}
    entry_root_set = set(roots)
    for dependency, dependent in edges:
        if dependency in scope and dependent in scope:
            adjacency.setdefault(dependency, []).append(dependent)
            predecessors.setdefault(dependent, []).append(dependency)
    adjacency[None] = list(roots)

    rpo = _reverse_postorder(adjacency, None, scope | {None})
    number = {node: index for index, node in enumerate(rpo)}
    idom = {index: None for index in range(len(rpo))}
    entry_number = number[None]
    idom[entry_number] = entry_number

    changed = True
    while changed:
        changed = False
        for node in rpo:
            if node is None:
                continue
            node_number = number[node]
            predecessor_numbers = [number[pred]
                                   for pred in predecessors.get(node, ())]
            if node in entry_root_set:
                predecessor_numbers.append(entry_number)
            new_idom = None
            for predecessor in predecessor_numbers:
                if idom[predecessor] is None:
                    continue
                if new_idom is None:
                    new_idom = predecessor
                else:
                    new_idom = _intersect(idom, new_idom, predecessor)
            if new_idom is not None and idom[node_number] != new_idom:
                idom[node_number] = new_idom
                changed = True

    result = {}
    ordered = []
    for node in rpo:
        if node is None:
            continue
        dominator_number = idom[number[node]]
        result[node] = None if dominator_number == entry_number else rpo[dominator_number]
        ordered.append(node)
    # ``ordered`` is a reverse postorder in which every dominator precedes
    # the nodes it dominates.
    return result, ordered


def audit_dependencies(roots, edges, blocked, component_ids=()):
    """Build the dependency_audit report from validated roots and edges.

    ``blocked`` is the set of component ids whose licence status is
    blocked.  ``component_ids`` is every known component id, so components
    untouched by the graph are reported as unreachable rather than
    silently dropped.  The graph is treated as immutable: blocked nodes
    keep their edges and remain reachable like any other component.

    All metrics come from one graph and one reachable scope.  Once
    immediate dominators are known, deactivating ``x`` disconnects exactly
    the nodes whose every root path passes through ``x`` -- precisely the
    dominator-tree subtree rooted at ``x`` -- so ``lost_count`` is the
    subtree size and no per-deactivation graph mutation is needed.
    """
    adjacency = {}
    for dependency, dependent in edges:
        adjacency.setdefault(dependency, []).append(dependent)

    root_set = list(dict.fromkeys(roots))
    scope = _reachable(adjacency, root_set)
    all_ids = set(component_ids)
    for dependency, dependent in edges:
        all_ids.add(dependency)
        all_ids.add(dependent)
    all_ids.update(root_set)
    unreachable = sorted(all_ids - scope)

    if not root_set:
        return {
            'roots': sorted(root_set),
            'unreachable': sorted(all_ids),
            'nodes': [],
        }

    idom, ordered = _immediate_dominators(root_set, edges, scope)

    # Dominator tree: parent is the immediate dominator (None for roots
    # and other entry-level nodes).
    children = {node: [] for node in scope}
    for node in ordered:
        parent = idom[node]
        if parent is not None:
            children[parent].append(node)

    # Iterative postorder of the dominator tree gives every subtree size
    # without recursion, so deep chains never touch the recursion limit.
    subtree_size = {}
    for start in ordered:
        if idom[start] is not None:
            continue
        stack = [(start, 0)]
        while stack:
            node, phase = stack.pop()
            if phase == 0:
                stack.append((node, 1))
                for child in children[node]:
                    stack.append((child, 0))
            else:
                subtree_size[node] = 1 + sum(subtree_size[child]
                                             for child in children[node])

    # Blocked dominator evidence accumulates along the dominator tree in
    # dominator-first order; a blocked node counts itself and is its own
    # nearest blocked dominator regardless of its ancestors.
    blocked_count = {}
    nearest_blocked = {}
    for node in ordered:
        parent = idom[node]
        if node in blocked:
            blocked_count[node] = (blocked_count[parent] + 1
                                   if parent is not None else 1)
            nearest_blocked[node] = node
        elif parent is not None:
            blocked_count[node] = blocked_count[parent]
            nearest_blocked[node] = nearest_blocked[parent]
        else:
            blocked_count[node] = 0
            nearest_blocked[node] = None

    nodes = [{
        'id': component_id,
        'immediate_dominator': idom[component_id],
        'lost_count': subtree_size[component_id],
        'blocked_dominator_count': blocked_count[component_id],
        'nearest_blocked_dominator': nearest_blocked[component_id],
    } for component_id in sorted(scope)]

    return {
        'roots': sorted(root_set),
        'unreachable': unreachable,
        'nodes': nodes,
    }
