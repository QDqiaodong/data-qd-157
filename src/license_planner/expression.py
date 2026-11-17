"""Small SPDX expression grammar; policy identifiers are caller supplied."""
import re

def parse(text):
    if not isinstance(text,str) or not text or len(text)>2000:raise ValueError('invalid expression length')
    tokens=re.findall(r'[A-Za-z0-9][A-Za-z0-9.+-]*|[()]',text)
    if ''.join(tokens)!=re.sub(r'\s+','',text) or len(tokens)>100:raise ValueError('invalid expression token/limit')
    i=0
    def atom():
        nonlocal i
        if i>=len(tokens):raise ValueError('expected license')
        t=tokens[i];i+=1
        if t=='(':
            tree=disjunction()
            if i>=len(tokens) or tokens[i]!=')':raise ValueError('unclosed expression')
            i+=1;return tree
        if t in ('AND','OR','WITH',')'):raise ValueError('expected license identifier')
        if i<len(tokens) and tokens[i]=='WITH':
            i+=1
            if i>=len(tokens) or tokens[i] in ('(',')','AND','OR','WITH'):raise ValueError('expected exception')
            t+=' WITH '+tokens[i];i+=1
        return ('license',t)
    def conjunction():
        nonlocal i
        tree=atom()
        while i<len(tokens) and tokens[i]=='AND':i+=1;tree=('AND',tree,atom())
        return tree
    def disjunction():
        nonlocal i
        tree=conjunction()
        while i<len(tokens) and tokens[i]=='OR':i+=1;tree=('OR',tree,conjunction())
        return tree
    result=disjunction()
    if i!=len(tokens):raise ValueError('trailing expression tokens')
    return result

def alternatives(tree):
    if tree[0]=='license':return {frozenset([tree[1]])}
    left,right=alternatives(tree[1]),alternatives(tree[2])
    if tree[0]=='AND':
        if len(left)*len(right)>4096:raise ValueError('license alternatives exceed bounded search')
        result={a|b for a in left for b in right}
    else:result=left|right
    if len(result)>256:raise ValueError('too many alternatives')
    return result
