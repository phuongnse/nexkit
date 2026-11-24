"""An automatic fix round whose change conflicts at its publish under a hold keeps
`rounds.automatic`; a person's push then a cancel of its checks gives the count back."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from eng import Item, chk, pub
it = Item({"fixRounds": 2, "conflicts": "resolve", "conflictRounds": 2})
it.send({"kind": "created"})
it.send({"kind": "requested", "what": "go"})
it.send({"kind": "actionFinished", "action": "runAgent", "result": "@agent:implement"})
it.send({"kind": "actionFinished", "action": "publish", "result": pub("308", "v1")})
it.send({"kind": "actionFinished", "action": "runChecks", "result": {"checks": [chk("test")]}})
it.send({"kind": "actionFinished", "action": "runAgent", "result": "@agent:review"})  # -> fixing, counted 1
it.send({"kind": "held"})
it.send({"kind": "actionFinished", "action": "runAgent", "result": "@agent:fix"})
it.send({"kind": "actionFinished", "action": "publish", "result": pub("308", "v2", conflicts=True)})
it.send({"kind": "changeObserved", "version": "v3"})   # a person's push resolves the conflict
it.send({"kind": "changeObserved", "version": "v4"})   # and pushes again while checks run
it.dump()
