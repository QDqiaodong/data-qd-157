"""Exact shared-obligation planning for a delivery release.

The batch objective is lexicographic:

1. sum of the costs of the union of selected obligations,
2. number of distinct obligations in that union,
3. sum of the number of selected licences over all components,
4. the sequence of per-component licence lists (component ids ascending,
   each list itself ascending) compared lexicographically.

With at most 16 distinct obligations we can decide every union mask exactly.
For a mask U, ``c_i(U)`` is the cheapest licence count component ``i`` can
contribute while keeping its obligations inside U (a subset-min zeta
transform).  ``S(U) = sum_i c_i(U)`` is therefore the least total licence
count of an assignment whose union is contained in U.  Minimising
``(cost(U), |U|, S(U))`` over all masks U yields the true optimum: if the
minimising assignment only used a strict subset, that subset would have an
equal or better key in every level, contradicting minimality of U (a strict
subset with equal size is impossible).  Consequently every combination of
per-component cheapest candidates for an optimal U has union exactly U, so
level 4 can be decided independently per component.
"""
INF = 10 ** 9
GINF = 10 ** 15


def _subset_min(counts_by_mask, bits):
    """arr[U] = min(counts_by_mask[m] for m subset of U), INF if none."""
    size = 1 << bits
    arr = [INF] * size
    for mask, count in counts_by_mask.items():
        if count < arr[mask]:
            arr[mask] = count
    for bit in range(bits):
        step = 1 << bit
        jump = step << 1
        for base in range(0, size, jump):
            for low in range(base, base + step):
                value = arr[low]
                if value < arr[low + step]:
                    arr[low + step] = value
    return arr


def plan_release(components, costs, obligations):
    """Return the release_plan report for id-sorted component descriptions.

    ``components`` entries are ``{'id', 'status', 'options'}`` where every
    option is ``(mask, licence_count, licences_tuple, obligations_tuple)``.
    ``obligations`` is the sorted list of all allowed policy obligations and
    gives the bit order used by the masks.
    """
    blocked = sorted(c['id'] for c in components if c['status'] != 'allowed')
    if blocked:
        return {
            'status': 'blocked',
            'blocked_components': blocked,
            'total_cost': None,
            'obligation_count': None,
            'license_occurrences': None,
            'selections': [],
            'obligations': [],
        }

    bits = len(obligations)
    if bits > 16:
        raise ValueError('too many policy obligations for release')
    size = 1 << bits

    # Components with the same (mask, count) option profile share c_i(U).
    profiles = {}
    for component in components:
        profile = tuple(sorted({(mask, count)
                                for mask, count, _licences, _obligations
                                in component['options']}))
        profiles[profile] = profiles.get(profile, 0) + 1

    # S[U] = least total licence count when all choices stay inside U.
    totals = [0] * size
    for profile, multiplicity in profiles.items():
        counts = {}
        for mask, count in profile:
            if count < counts.get(mask, INF):
                counts[mask] = count
        cheapest = _subset_min(counts, bits)
        for mask in range(size):
            if totals[mask] >= GINF:
                continue
            value = cheapest[mask]
            if value >= INF:
                totals[mask] = GINF
            else:
                totals[mask] += multiplicity * value

    cost_by_bit = [costs[name] for name in obligations]
    union_cost = [0] * size
    for mask in range(1, size):
        low = mask & -mask
        union_cost[mask] = (union_cost[mask ^ low]
                            + cost_by_bit[low.bit_length() - 1])

    best_key = None
    best_masks = []
    for mask in range(size):
        if totals[mask] >= GINF:
            continue
        key = (union_cost[mask], mask.bit_count(), totals[mask])
        if best_key is None or key < best_key:
            best_key = key
            best_masks = [mask]
        elif key == best_key:
            best_masks.append(mask)

    # Options are scanned in (licence count, licence list) order, so for a
    # candidate union the first feasible option is the level-3/4 optimum.
    ordered = [[option for option in
                sorted(component['options'],
                       key=lambda item: (item[1], item[2]))]
               for component in components]

    best_sequence = None
    best_picks = None
    for mask in best_masks:
        sequence = []
        picks = []
        used = 0
        total_licences = 0
        suffix_free = False      # a strict prefix win needs no more comparison
        for index, options in enumerate(ordered):
            option = next(item for item in options if item[0] & ~mask == 0)
            used |= option[0]
            total_licences += option[1]
            licences = list(option[2])
            picks.append((components[index]['id'], option))
            if not suffix_free:
                if best_sequence is None:
                    pass
                elif licences < best_sequence[index]:
                    suffix_free = True
                elif licences > best_sequence[index]:
                    break
            sequence.append(licences)
        else:
            if used != mask or total_licences != totals[mask]:
                raise ValueError('internal release plan inconsistency')
            if best_sequence is None or sequence < best_sequence:
                best_sequence = sequence
                best_picks = picks

    picks = best_picks
    selections = []
    needed = {}
    for component_id, (_mask, _count, licences, mandatory) in picks:
        selections.append({
            'id': component_id,
            'licenses': list(licences),
            'obligations': list(mandatory),
        })
        for obligation in mandatory:
            needed.setdefault(obligation, set()).add(component_id)

    obligation_report = [
        {
            'obligation': obligation,
            'cost': costs[obligation],
            'components': sorted(needed[obligation]),
        }
        for obligation in sorted(needed)
    ]
    return {
        'status': 'allowed',
        'blocked_components': [],
        'total_cost': sum(costs[name] for name in needed),
        'obligation_count': len(needed),
        'license_occurrences': sum(len(item['licenses'])
                                    for item in selections),
        'selections': selections,
        'obligations': obligation_report,
    }
