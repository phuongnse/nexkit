"""Per differing scenario: nameset size, step, and repeated / route diagnose difference."""
import json, sys, collections
a={json.loads(l)['index']:json.loads(l) for l in open(sys.argv[1])}; b={json.loads(l)['index']:json.loads(l) for l in open(sys.argv[2])}
sc={json.loads(l)['index']:json.loads(l) for l in open(sys.argv[3])}
def st(s, pr):
    cs=[x for x in s['gh']['comments'].get(pr,[]) if '<STATE' in x]
    if not cs: return {}
    c=cs[0]; return json.loads(c[c.find('<STATE')+7:c.rfind('>')])
cnt=collections.Counter(); ex={}
for i in sorted(a):
    if a[i]==b[i]: continue
    names=len(sc[i]['names'])
    for k,(s1,s2) in enumerate(zip(a[i]['steps'],b[i]['steps'])):
        if s1!=s2:
            pr='7' if s1['kind']=='person_pr' else '6'
            r1=st(s1,pr).get('feedback',{}).get('repeated'); r2=st(s2,pr).get('feedback',{}).get('repeated')
            d1=s1['routes'].get('true'); d2=s2['routes'].get('true')
            other=[key for key in s1 if s1[key]!=s2.get(key)]
            key=(names>30, r1!=r2, d1!=d2, tuple(other))
            cnt[key]+=1; ex.setdefault(key,(i,k,s1['kind'],r1,r2,d1,d2))
            break
for k,n in cnt.most_common(): print(n,'many-names' if k[0] else 'few-names','repeated-differs' if k[1] else '', 'diagnose-differs' if k[2] else '', k[3]); print('   e.g.', ex[k])
