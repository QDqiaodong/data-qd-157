from .common import require,ident,rows
from .expression import parse,alternatives

def analyze(data):
    require(data,'policy components');policy={}
    for r in rows(data['policy'],200):
        require(r,'license allowed obligations');key=ident(r['license']);tree=parse(key)
        if tree!=('license',key) or key in policy or type(r['allowed']) is not bool:raise ValueError('invalid policy key/allow flag')
        obligations=[ident(x) for x in rows(r['obligations'],50)]
        if len(set(obligations))!=len(obligations):raise ValueError('duplicate obligation')
        policy[key]=(r['allowed'],set(obligations))
    components=[];seen=set()
    for r in rows(data['components'],1000):
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
    return {'compliant':all(c['status']=='allowed' for c in components),'components':sorted(components,key=lambda c:c['id'])}
