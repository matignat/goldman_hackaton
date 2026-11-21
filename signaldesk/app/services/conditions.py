"""Schema-agnostic field paths, typed condition tree, safe presentation templates."""
import re
import fnmatch
from datetime import datetime, timezone
from typing import Any

MISSING=object()
SEVERITY={'debug': 'info','trace':'info','info':'info','information':'info','warn':'warning',
          'warning':'warning','error':'error','err':'error','fatal':'critical','critical':'critical','sev1':'critical'}

def flatten(value:Any,prefix:str='',result:dict|None=None):
    if result is None:result={}
    if isinstance(value,dict):
        for k,v in value.items():flatten(v,f'{prefix}.{k}' if prefix else str(k),result)
    elif isinstance(value,list):
        result[prefix]=value
        for i,v in enumerate(value):flatten(v,f'{prefix}.{i}' if prefix else str(i),result)
    else:result[prefix]=value
    return result

def get_field(payload:dict,path:str,default=MISSING):
    if path in payload:return payload[path]
    value=payload
    for key in str(path).split('.'):
        if isinstance(value,dict):value=value.get(key,MISSING)
        elif isinstance(value,list) and key.isdigit() and int(key)<len(value):value=value[int(key)]
        else:return default
        if value is MISSING:return default
    return value

def coerce(value):
    if isinstance(value,(float,int,bool)):return value
    if isinstance(value,str):
        try:return float(value)
        except (ValueError,OverflowError):pass
        try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        except ValueError:pass
    return str(value)

def compare(a,b,op):
    if op in ('exists','not_exists'):return (a is not MISSING and a is not None)==(op=='exists')
    if a is MISSING:return False
    if op in ('eq','=','equals'):return coerce(a)==coerce(b)
    if op in ('ne','!=','not_equals'):return coerce(a)!=coerce(b)
    if op in ('gt','gte','lt','lte'):
        a,b=coerce(a),coerce(b)
        try:
            return {'gt':lambda:a>b,'gte':lambda:a>=b,'lt':lambda:a<b,'lte':lambda:a<=b}[op]()
        except TypeError:return False
    if op=='in':return a in b if isinstance(b,(list,tuple)) else False
    if op=='contains':return str(b).lower() in str(a).lower()
    if op=='starts_with':return str(a).lower().startswith(str(b).lower())
    if op=='wildcard':return fnmatch.fnmatchcase(str(a).lower(),str(b).lower())
    if op=='regex':
        if len(str(b))>200:return False
        try:return re.search(str(b),str(a),re.IGNORECASE) is not None
        except re.error:return False
    raise ValueError(f'Unsupported operator: {op}')

def validate(condition,depth=0):
    if depth>20:raise ValueError('Too many nested condition groups (max 20)')
    if not isinstance(condition,dict):raise ValueError('Condition must be an object')
    if not condition:return
    if 'all' in condition or 'any' in condition:
        for key in ('all','any'):
            if key in condition:
                if not isinstance(condition[key],list):raise ValueError(f'{key} must be a list')
                for child in condition[key]:validate(child,depth+1)
        return
    if 'not' in condition:return validate(condition['not'],depth+1)
    if not isinstance(condition.get('field'),str) or not condition['field']:
        raise ValueError('Leaf condition requires field')
    if condition.get('op','eq') not in {'eq','equals','=','ne','not_equals','!=','gt','gte','lt','lte','in','contains','starts_with','wildcard','regex','exists','not_exists'}:
        raise ValueError('Unsupported condition operator')
    if condition.get('op')=='regex':
        try:re.compile(condition.get('value',''))
        except re.error as e:raise ValueError(f'Invalid regex: {e}') from e

def matches(condition:dict,fields:dict)->bool:
    if not condition:return True
    if 'all' in condition:return all(matches(c,fields) for c in condition['all'])
    if 'any' in condition:return any(matches(c,fields) for c in condition['any'])
    if 'not' in condition:return not matches(condition['not'],fields)
    return compare(get_field(fields,condition['field']),condition.get('value'),condition.get('op','eq'))

_TEMPLATE=re.compile(r'\{\{\s*([\w.\-]+)\s*\}\}')
def render(template:str,fields:dict)->str:
    def replace(match):
        value=get_field(fields,match.group(1))
        return '[missing]' if value is MISSING else str(value)
    return _TEMPLATE.sub(replace,template or '')[:10000]

def canonical(payload:Any,source_cfg:dict,received_at:datetime)->dict:
    cfg=source_cfg or {}
    if received_at.tzinfo is None:received_at=received_at.replace(tzinfo=timezone.utc)
    if isinstance(payload,str) and cfg.get('pattern'):
        m=re.search(cfg['pattern'],payload)
        if m:payload={'raw_line':payload,**m.groupdict()}
    fields=flatten(payload)
    mappings=cfg.get('mappings',{})
    def pick(name,candidates):
        for key in [*([mappings[name]] if name in mappings else []),*candidates]:
            value=get_field(fields,key)
            if value is not MISSING and value is not None:return value
        return None
    raw_severity=pick('severity',['severity','labels.severity','level','priority']) or 'info'
    severity=SEVERITY.get(str(raw_severity).lower(),'info')
    time_value=pick('occurredAt',['timestamp','startsAt','meta.ts','time','occurredAt'])
    occurred=received_at
    if time_value is not None:
        try:
            if isinstance(time_value,(int,float)) or str(time_value).replace('.','',1).isdigit():
                n=float(time_value);occurred=datetime.fromtimestamp(n/1000 if abs(n)>1e11 else n,tz=timezone.utc)
            else:occurred=datetime.fromisoformat(str(time_value).replace('Z','+00:00'))
            if not occurred.tzinfo:occurred=occurred.replace(tzinfo=timezone.utc)
        except (OverflowError,ValueError,TypeError):occurred=received_at
    schema=pick('schema',['meta.schema','type','alertname','schema']) or 'unknown'
    service=pick('service',['labels.service','workflowName','meta.producer','service','application']) or ''
    status=pick('status',['status','state','incident.status']) or 'firing'
    environment=pick('environment',['environment','labels.environment','env']) or ''
    output={**fields,'canonical.severity':severity,'canonical.schema':str(schema),
            'canonical.service':str(service),'canonical.status':str(status).lower(),
            'canonical.environment':str(environment),'canonical.occurredAt':occurred.isoformat()}
    return {'flat':output,'severity':severity,'occurred_at':occurred,'schema':str(schema),
            'service':str(service),'status':str(status).lower(),'environment':str(environment)}
