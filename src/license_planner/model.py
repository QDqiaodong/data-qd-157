from .common import require,ident,number,rows
from .expression import parse,alternatives
from .release import plan_release
from .dependency import validate_graph,audit_dependencies,COMPONENT_LIMIT,EDGE_LIMIT

_MISSING=object()
_ALLOWED_FIELDS=frozenset(('policy','components','release','dependency_graph'))

def _shape(data):
    if not isinstance(data,dict) or not set(data)<=_ALLOWED_FIELDS or 'policy' not in data or 'components' not in data:
        raise ValueError('expected object fields: policy components[ release][ dependency_graph]')
    return data.get('release',_MISSING),data.get('dependency_graph',_MISSING)

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
    release,graph_value=_shape(data)
    has_graph=graph_value is not _MISSING
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
    return result
