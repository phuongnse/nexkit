import sys,time,json
sys.argv=['x',sys.argv[1],'/dev/null']
import runpy
m=runpy.run_path('$SCRATCH/r341/hreport.py',run_name='h')
sc=next(m['scenarios']())
t=time.time()
for i,sc in zip(range(30),m['scenarios']()):
    r=m['run'](sc)
print((time.time()-t)/30)
print(json.dumps(r)[:3000])
