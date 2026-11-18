"""Exact cross-version component lineage reconciliation.

The matching objective is deliberately isolated from every licence
judgement: candidate edges and their caller-supplied penalties are the only
inputs.  It is a two-level objective:

1. maximise the number of matched baseline/current pairs (exactly),
2. among those maximum matchings minimise the sum of penalties (exactly).

No lexicographic tie-break is applied to the matching itself.  Instead
every supplied edge is classified against the *whole* set of optimal
matchings:

* ``forced``   -- present in every optimal matching,
* ``possible`` -- present in at least one, but not every, optimal matching,
* ``excluded`` -- present in no optimal matching.

The classification is obtained from one min-cost maximum-flow solve.  Let
``f`` be one optimal integral flow.  Every other flow of the same value
differs from ``f`` by a circulation in the residual graph.  Because ``f``
is optimal, the residual graph has no negative cycle; with a globally
feasible potential every residual arc has non-negative reduced cost, and an
alternative optimal flow is therefore exactly a circulation using only
zero-reduced-cost residual arcs.  Consequently an original edge lies on a
zero-reduced-cost residual cycle precisely when it can be exchanged into
another optimal matching.  Strongly connected components of that zero-cost
residual graph decide all three classes without re-solving a matching per
edge.  This is exact and uses one flow solve plus one SCC pass.
"""

from collections import deque
import heapq

from .common import ident, number, require, rows

BASELINE_COMPONENT_LIMIT = 1000
LINK_LIMIT = 12000
INF = 10 ** 30


def parse_links(value, baseline_ids, current_ids):
    """Validate the ordered candidate-edge array.

    Every entry has exactly ``baseline_id``, ``current_id`` and an integer
    ``penalty`` in 0..1000000000 (``bool`` is rejected by the shared
    ``number`` helper).  Both endpoints must reference accepted records on
    their own side; duplicate ordered pairs are rejected.  Names shared by
    the two versions are never paired implicitly -- only supplied edges
    exist.
    """
    links = []
    seen = set()
    for entry in rows(value, LINK_LIMIT):
        require(entry, 'baseline_id current_id penalty')
        baseline_id = ident(entry['baseline_id'])
        current_id = ident(entry['current_id'])
        penalty = number(entry['penalty'])
        if baseline_id not in baseline_ids or current_id not in current_ids:
            raise ValueError('reconciliation link must reference existing baseline and current ids')
        pair = (baseline_id, current_id)
        if pair in seen:
            raise ValueError('duplicate reconciliation link')
        seen.add(pair)
        links.append((baseline_id, current_id, penalty))
    return links


class _ResidualGraph:
    """Integer min-cost flow network with paired forward/reverse arcs."""

    __slots__ = ('graph', 'frm', 'to', 'cap', 'cost')

    def __init__(self, size):
        self.graph = [[] for _ in range(size)]
        self.frm = []
        self.to = []
        self.cap = []
        self.cost = []

    def add(self, source, target, capacity, cost):
        edge = len(self.to)
        self.graph[source].append(edge)
        self.frm.append(source)
        self.to.append(target)
        self.cap.append(capacity)
        self.cost.append(cost)
        self.graph[target].append(edge + 1)
        self.frm.append(target)
        self.to.append(source)
        self.cap.append(0)
        self.cost.append(-cost)
        return edge


def _min_cost_max_flow(network, source, sink):
    """Successive shortest augmenting paths; returns (flow, cost).

    All original costs are non-negative, so the zero potential is feasible
    and Dijkstra with reduced costs is valid throughout.  Unit capacities
    keep every augmentation integral.
    """
    graph, to, cap, cost = network.graph, network.to, network.cap, network.cost
    size = len(graph)
    potential = [0] * size
    flow = 0
    total_cost = 0
    while True:
        distance = [INF] * size
        previous_node = [-1] * size
        previous_edge = [-1] * size
        distance[source] = 0
        heap = [(0, source)]
        while heap:
            value, node = heapq.heappop(heap)
            if value != distance[node]:
                continue
            for edge in graph[node]:
                if cap[edge] <= 0:
                    continue
                target = to[edge]
                candidate = value + cost[edge] + potential[node] - potential[target]
                if candidate < distance[target]:
                    distance[target] = candidate
                    previous_node[target] = node
                    previous_edge[target] = edge
                    heapq.heappush(heap, (candidate, target))
        if distance[sink] >= INF:
            break
        for node, value in enumerate(distance):
            if value < INF:
                potential[node] += value
        augment = INF
        node = sink
        while node != source:
            edge = previous_edge[node]
            if cap[edge] < augment:
                augment = cap[edge]
            node = previous_node[node]
        node = sink
        path_cost = 0
        while node != source:
            edge = previous_edge[node]
            cap[edge] -= augment
            cap[edge ^ 1] += augment
            path_cost += cost[edge]
            node = previous_node[node]
        flow += augment
        total_cost += augment * path_cost
    return flow, total_cost


def _feasible_potentials(network):
    """Globally feasible potentials for every residual arc.

    The potentials maintained by successive shortest paths are feasible on
    the arcs reachable from the source, but an optimal flow can leave a
    region that is not source-reachable.  A super-source with zero-cost
    arcs to every node yields shortest distances that make every residual
    reduced cost non-negative.  The optimal residual graph has no negative
    cycle, so these distances exist.
    """
    graph, frm, to, cap, cost = (network.graph, network.frm, network.to,
                                 network.cap, network.cost)
    size = len(graph)
    distance = [0] * size
    queued = [True] * size
    queue = deque(range(size))
    while queue:
        node = queue.popleft()
        queued[node] = False
        base = distance[node]
        for edge in graph[node]:
            if cap[edge] <= 0:
                continue
            target = to[edge]
            candidate = base + cost[edge]
            if candidate < distance[target]:
                distance[target] = candidate
                if not queued[target]:
                    queued[target] = True
                    queue.append(target)
    return distance


def _strong_components(adjacency):
    """Iterative Kosaraju SCC ids over an arbitrary directed graph."""
    size = len(adjacency)
    reverse = [[] for _ in range(size)]
    for node, targets in enumerate(adjacency):
        for target in targets:
            reverse[target].append(node)

    visited = [False] * size
    order = []
    for start in range(size):
        if visited[start]:
            continue
        visited[start] = True
        stack = [(start, 0)]
        while stack:
            node, index = stack[-1]
            targets = adjacency[node]
            if index < len(targets):
                target = targets[index]
                stack[-1] = (node, index + 1)
                if not visited[target]:
                    visited[target] = True
                    stack.append((target, 0))
            else:
                order.append(node)
                stack.pop()

    component = [-1] * size
    current = 0
    for start in reversed(order):
        if component[start] != -1:
            continue
        component[start] = current
        stack = [start]
        while stack:
            node = stack.pop()
            for target in reverse[node]:
                if component[target] == -1:
                    component[target] = current
                    stack.append(target)
        current += 1
    return component


def audit_matching(links, baseline_status, current_status):
    """Build the complete reconciliation audit for validated links.

    ``links`` are ``(baseline_id, current_id, penalty)`` triples;
    ``baseline_status``/``current_status`` map record ids to the existing
    per-component ``allowed``/``blocked`` judgement.  Sorting and the
    forced-only transition list make the report invariant under input
    order permutations.
    """
    baseline_ids = sorted(baseline_status)
    current_ids = sorted(current_status)
    ordered_links = sorted(links, key=lambda item: (item[0], item[1]))

    if not ordered_links:
        return {
            'matched_count': 0,
            'minimum_penalty': 0,
            'links': [],
            'always_unmatched_baseline': baseline_ids,
            'always_unmatched_current': current_ids,
            'transitions': [],
        }

    baseline_index = {name: index for index, name in enumerate(baseline_ids)}
    current_index = {name: index for index, name in enumerate(current_ids)}
    baseline_count = len(baseline_ids)
    source = 0
    baseline_offset = 1
    current_offset = baseline_offset + baseline_count
    sink = current_offset + len(current_ids)
    network = _ResidualGraph(sink + 1)

    for index in range(baseline_count):
        network.add(source, baseline_offset + index, 1, 0)
    for index in range(len(current_ids)):
        network.add(current_offset + index, sink, 1, 0)

    edge_of_pair = {}
    for baseline_id, current_id, penalty in ordered_links:
        edge = network.add(baseline_offset + baseline_index[baseline_id],
                           current_offset + current_index[current_id],
                           1, penalty)
        edge_of_pair[(baseline_id, current_id)] = edge

    flow, minimum_penalty = _min_cost_max_flow(network, source, sink)
    potential = _feasible_potentials(network)

    cap, cost, frm, to = network.cap, network.cost, network.frm, network.to
    zero_graph = [[] for _ in range(len(network.graph))]
    for edge in range(len(to)):
        if cap[edge] > 0 and cost[edge] + potential[frm[edge]] - potential[to[edge]] == 0:
            zero_graph[frm[edge]].append(to[edge])
    component = _strong_components(zero_graph)

    classified = []
    unmatched_baseline = {name: True for name in baseline_ids}
    unmatched_current = {name: True for name in current_ids}
    transitions = []
    for baseline_id, current_id, penalty in ordered_links:
        edge = edge_of_pair[(baseline_id, current_id)]
        source_node = frm[edge]
        target_node = to[edge]
        if cap[edge] == 0:
            # The forward arc is saturated: this edge is used by the flow.
            # It can be dropped from an optimal matching iff its reverse
            # residual arc lies on a zero-reduced-cost cycle.
            reverse = edge ^ 1
            exchangeable = (cap[reverse] > 0
                            and cost[reverse] + potential[target_node] - potential[source_node] == 0
                            and component[source_node] == component[target_node])
            classification = 'possible' if exchangeable else 'forced'
        else:
            exchangeable = (cost[edge] + potential[source_node] - potential[target_node] == 0
                            and component[source_node] == component[target_node])
            classification = 'possible' if exchangeable else 'excluded'

        classified.append({
            'baseline_id': baseline_id,
            'current_id': current_id,
            'penalty': penalty,
            'classification': classification,
        })
        if classification != 'excluded':
            unmatched_baseline[baseline_id] = False
            unmatched_current[current_id] = False
        if classification == 'forced':
            transitions.append({
                'baseline_id': baseline_id,
                'current_id': current_id,
                'baseline_status': baseline_status[baseline_id],
                'current_status': current_status[current_id],
            })

    transitions.sort(key=lambda item: (item['baseline_id'], item['current_id']))
    return {
        'matched_count': flow,
        'minimum_penalty': minimum_penalty,
        'links': classified,
        'always_unmatched_baseline': [name for name in baseline_ids
                                      if unmatched_baseline[name]],
        'always_unmatched_current': [name for name in current_ids
                                     if unmatched_current[name]],
        'transitions': transitions,
    }
