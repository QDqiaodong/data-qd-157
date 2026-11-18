from .common import require,ident,number,rows
from .expression import parse,alternatives
from .release import plan_release
from .dependency import validate_graph,audit_dependencies,COMPONENT_LIMIT
from .reconcile import (BASELINE_COMPONENT_LIMIT,audit_matching,
                        parse_links)

_MISSING=object()
_ALLOWED_FIELDS=frozenset(('policy','components','release','dependency_graph','reconciliation'))
_BASELINE_POLICY_LIMIT=200
_BASELINE_OBLIGATION_LIMIT=50

def _baseline_side(reconciliation):
    """Validate reconciliation.baseline and return its per-component status.

    ``baseline`` contains only ``policy`` and ``components`` -- nesting a
    further reconciliation or any other analysis is rejected.  The policy
    rules (identifiers, SPDX key shape, unique obligations) are reused
    verbatim; components carry only ``id`` and ``expression``.  The status
    is decided exactly like the current side: a component is allowed iff
    at least one alternative uses only known, allowed policy keys.
    """
    require(reconciliation,'baseline links')
    baseline=reconciliation['baseline']
    if not isinstance(baseline,dict) or set(baseline)!={'policy','components'}:
        raise ValueError('reconciliation baseline must contain exactly policy and components')
    policy={}
    for r in rows(baseline['policy'],_BASELINE_POLICY_LIMIT):
        require(r,'license allowed obligations');key=ident(r['license']);tree=parse(key)
        if tree!=('license',key) or key in policy or type(r['allowed']) is not bool:
            raise ValueError('invalid policy key/allow flag')
        obligations=[ident(x) for x in rows(r['obligations'],_BASELINE_OBLIGATION_LIMIT)]
        if len(set(obligations))!=len(obligations):raise ValueError('duplicate obligation')
        policy[key]=(r['allowed'],set(obligations))
    legacy={}
    for r in rows(baseline['components'],BASELINE_COMPONENT_LIMIT):
        if not isinstance(r,dict) or set(r)!={'id','expression'}:
            raise ValueError('baseline component must contain exactly id and expression')
        cid=ident(r['id'])
        if cid in legacy:raise ValueError('duplicate baseline component')
        choices=alternatives(parse(r['expression']))
        allowed=any(all(key in policy and policy[key][0] for key in choice)
                    for choice in choices)
        legacy[cid]='allowed' if allowed else 'blocked'
    return legacy

def _reconciliation(value,current_status):
    """Validate the optional root reconciliation and audit its links."""
    legacy=_baseline_side(value)
    links=parse_links(value['links'],set(legacy),set(current_status))
    return audit_matching(links,legacy,current_status)

def _shape(data):
    if not isinstance(data,dict) or not set(data)<=_ALLOWED_FIELDS or 'policy' not in data or 'components' not in data:
        raise ValueError('expected object fields: policy components[ release][ dependency_graph][ reconciliation]')
    return (data.get('release',_MISSING),data.get('dependency_graph',_MISSING),
            data.get('reconciliation',_MISSING))

def _costs(release,allowed_obligations):
    require(release,'obligation_costs')
    costs={}
    for r in rows(release['obligation_costs'],1000):
        require(r,'obligation cost');name=ident(r['obligation']);cost=number(r['cost'])
        if name in costs:raise ValueError('duplicate obligation cost')
        costs[name]=cost
    missing=allowed_obligations-set(costs);unknown=set(costs)-allowed_obligations
    if missing or unknown:raise ValueError('obligation costs must cover exactly the allowed obligations')
    return costs

def analyze(data):
    release,graph_value,reconciliation_value=_shape(data)
    has_graph=graph_value is not _MISSING
    has_reconciliation=reconciliation_value is not _MISSING
    policy={}
    for r in rows(data['policy'],200):
        require(r,'license allowed obligations');key=ident(r['license']);tree=parse(key)
        if tree!=('license',key) or key in policy or type(r['allowed']) is not bool:raise ValueError('invalid policy key/allow flag')
        obligations=[ident(x) for x in rows(r['obligations'],50)]
        if len(set(obligations))!=len(obligations):raise ValueError('duplicate obligation')
        policy[key]=(r['allowed'],set(obligations))
    obligations=sorted(set().union(*(duties for allowed,duties in policy.values() if allowed)))
    costs=None if release is _MISSING else _costs(release,set(obligations))
    if costs is not None and len(obligations)>16:raise ValueError('too many policy obligations for release')
    if costs is not None:
        component_limit=120
    elif has_graph:
        component_limit=COMPONENT_LIMIT
    else:
        component_limit=1000
    if has_reconciliation:
        component_limit=min(component_limit,BASELINE_COMPONENT_LIMIT)
    components=[];seen=set()
    for r in rows(data['components'],component_limit):
        require(r,'id expression');cid=ident(r['id'])
        if cid in seen:raise ValueError('duplicate component')
        seen.add(cid);choices=alternatives(parse(r['expression']));permitted=[];rejected=[]
        for choice in sorted(choices,key=lambda s:tuple(sorted(s))):
            blocked=sorted(k for k in choice if k not in policy or not policy[k][0])
            if blocked:rejected.append({'licenses':sorted(choice),'blocked_or_unknown':blocked});continue
            duties=set().union(*(policy[k][1] for k in choice))
            permitted.append({'licenses':sorted(choice),'obligations':sorted(duties)})
        permitted.sort(key=lambda x:(len(x['obligations']),len(x['licenses']),x['licenses']))
        components.append({'id':cid,'status':'allowed' if permitted else 'blocked','selected':permitted[0] if permitted else None,'alternatives':permitted,'rejected':rejected})
    components.sort(key=lambda c:c['id'])
    result={'compliant':all(c['status']=='allowed' for c in components),'components':components}
    if costs is not None:
        bit={name:index for index,name in enumerate(obligations)}
        described=[]
        for c in components:
            options=[(sum(1<<bit[x] for x in option['obligations']),len(option['licenses']),tuple(option['licenses']),tuple(option['obligations'])) for option in c['alternatives']]
            described.append({'id':c['id'],'status':c['status'],'options':options})
        result['release_plan']=plan_release(described,costs,obligations)
    if has_graph:
        roots,edges=validate_graph(graph_value,seen)
        blocked={c['id'] for c in components if c['status']!='allowed'}
        result['dependency_audit']=audit_dependencies(roots,edges,blocked,seen)
    if has_reconciliation:
        result['reconciliation_audit']=_reconciliation(
            reconciliation_value,
            {c['id']:c['status'] for c in components})
    return result
