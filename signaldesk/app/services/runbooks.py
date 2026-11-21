"""Format adapters and safe, mock-by-default action handlers."""
import json,re
from urllib.parse import urlparse
import yaml
from .conditions import render

class FormatRegistry:
    def __init__(self):self.parsers={}
    def register(self,name,parser):self.parsers[name]=parser
    def parse(self,name,text):
        parser=self.parsers.get(name)
        try: steps=parser(text) if parser else []
        except Exception:steps=[]
        return steps or [{'text':text,'type':'manual','approval':'none'}]

formats=FormatRegistry()

def structured(text):
    doc=yaml.safe_load(text)
    if isinstance(doc,str):return [{'text':doc,'type':'manual','approval':'none'}]
    steps=(doc.get('steps') or []) if isinstance(doc,dict) else doc
    output=[]
    for step in steps:
        if isinstance(step,str):output.append({'text':step,'type':'manual','approval':'none'});continue
        if not isinstance(step,dict):continue
        action=step.get('action')
        output.append({'text':str(step.get('check') or step.get('text') or action or 'Complete step'),
                       'type':'action' if action else 'manual',
                       'action':action,'params':{k:v for k,v in step.items() if k not in ('text','check','action','approval','if')},
                       'condition':step.get('if'),'approval':str(step.get('approval','none')).lower()})
    return output

def markdown(text):
    result=[]
    for line in text.splitlines():
        match=re.match(r'^\s*(?:\d+\.|[-*]\s*(?:\[[ xX]\])?)\s+(.+)',line)
        if not match:continue
        item=match.group(1).strip()
        approval='required' if re.search(r'needs approval|require[s]? approval|manager approval|team lead',item,re.I) else 'none'
        result.append({'text':item,'type':'manual','approval':approval})
    return result

def link(text):
    parsed=urlparse(text.strip())
    return [{'text':'Follow the linked runbook: '+text.strip(),'type':'manual','approval':'none'}] if parsed.scheme in ('https','http') else []
formats.register('yaml',structured)
formats.register('json',lambda t:structured(json.dumps(json.loads(t))))
formats.register('markdown',markdown)
formats.register('checklist',markdown)
formats.register('link',link)
formats.register('text',lambda t:[{'text':t,'type':'manual','approval':'none'}])

class HandlerRegistry:
    def __init__(self):self.handlers={}
    def register(self,name,handler):self.handlers[name]=handler
    def execute(self,name,params,ticket,db):
        handler=self.handlers.get(name)
        if not handler:raise ValueError(f'Unknown action {name}; blocked')
        return handler(params,ticket,db)

actions=HandlerRegistry()
def mock_action(name):
    return lambda params,ticket,db:{'status':'simulated','action':name,'message':'Demo safe simulation; no external mutation'}
for _name in ('retry_workflow','restart_service','http_get','webhook','open_ticket','send_message','http_post'):
    actions.register(_name,mock_action(_name))

def escalate(params,ticket,db):
    from ..models import Team
    target=params.get('team')
    if target:
        team=db.query(Team).filter((Team.slug==str(target))|(Team.name==str(target))).first()
        if team:
            before=ticket.team_id;ticket.team_id=team.id
            return {'status':'simulated_transfer','from_team':before,'to_team':team.id}
    return {'status':'manual_escalation_required','target':target}
actions.register('escalate',escalate)

def ticket_steps(runbook,flat):
    return [{**s,'text':render(s.get('text',''),flat),'params':{k:render(v,flat) if isinstance(v,str) else v for k,v in s.get('params',{}).items()},'status':'pending','approved_by':None,'done_by':None} for s in (runbook.steps or [])] if runbook else []
