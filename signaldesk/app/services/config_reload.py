"""Manager-authorized, atomic, live file-based rule reload.

Configuration files are an additional WRITE PATH, never a route around team RBAC.
The manager signs exactly the intended JSON using their authenticated API session;
background workers verify the signature and current team leadership before publishing.
Invalid/replayed-bad manifests have no side effects; last good rules stay enabled.
"""
import hashlib,hmac,json,logging,os
from pathlib import Path
from sqlalchemy.orm import Session
from ..models import User,Team,Rule,Runbook
from .conditions import validate
from .records import audit

log=logging.getLogger('signaldesk.config')
KNOWN={'name','description','priority','enabled','condition','presentation','routing',
       'dedup_template','group_template','throttle_seconds','runbook_id'}
REQUIRED={'name','condition'}


def canonical_bytes(manifest:dict)->bytes:
    value={key:manifest[key] for key in ('team_id','manager_id','rules','disable_missing','issued_at')}
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')


def sign_manifest(manifest:dict)->dict:
    from ..auth import SIGNER
    secret=os.getenv('SECRET_KEY','dev-secret-for-local-demo-only')
    digest=hmac.new(secret.encode(),canonical_bytes(manifest),hashlib.sha256).hexdigest()
    return {**manifest,'signature':digest}


def verify_manifest(manifest:dict)->None:
    from datetime import datetime,timezone,timedelta
    required=('team_id','manager_id','rules','disable_missing','issued_at','signature')
    if not isinstance(manifest,dict) or any(k not in manifest for k in required):
        raise ValueError('Missing signed-manifest fields')
    actual=manifest['signature']
    if not isinstance(actual,str):raise ValueError('Invalid signature format')
    expected=sign_manifest({k:manifest[k] for k in required if k!='signature'})['signature']
    if not hmac.compare_digest(actual,expected):raise ValueError('Invalid manager signature')
    issued=datetime.fromisoformat(manifest['issued_at'].replace('Z','+00:00'))
    if issued.tzinfo is None:raise ValueError('Manifest timestamp must have timezone')
    current=datetime.now(timezone.utc)
    if issued>current+timedelta(minutes=5) or issued<current-timedelta(days=1):
        raise ValueError('Authorization expired: request a new signed manifest')


def validate_manifest(db:Session,manifest:dict)->list[dict]:
    verify_manifest(manifest)
    team=db.get(Team,int(manifest['team_id']))
    user=db.get(User,int(manifest['manager_id']))
    if not team or not user or team.leader_id!=user.id or user.role!='M':
        raise PermissionError('Signer is not the current designated team manager')
    rules=manifest['rules']
    if not isinstance(rules,list) or not rules or len(rules)>1000:
        raise ValueError('Provide between 1 and 1000 rules')
    unique=set();clean=[]
    for obj in rules:
        if not isinstance(obj,dict) or not REQUIRED<=set(obj):raise ValueError('Each rule needs name and condition')
        name=str(obj['name']).strip()
        if not name or len(name)>120 or name in unique:raise ValueError('Duplicate/invalid rule name')
        unique.add(name);validate(obj['condition'])
        if not isinstance(obj.get('presentation',{}),dict) or not isinstance(obj.get('routing',{}),dict):
            raise ValueError('Presentation and routing must be objects')
        rb_id=obj.get('runbook_id')
        if rb_id is not None:
            rb=db.get(Runbook,int(rb_id))
            if not rb or rb.team_id!=team.id:raise ValueError('Runbook is not owned by this team')
        clean.append({k:v for k,v in obj.items() if k in KNOWN})
    return clean


def apply_signed_manifest(db:Session,manifest:dict)->dict:
    rules=validate_manifest(db,manifest)  # entire manifest checked before ANY mutation
    tid=int(manifest['team_id']);actor=db.get(User,int(manifest['manager_id']))
    current={r.name:r for r in db.query(Rule).filter_by(team_id=tid)}
    changed=0
    for data in rules:
        name=data['name'];old=current.get(name)
        if old:
            dirty=any(getattr(old,k)!=v for k,v in data.items())
            if dirty:
                for key,val in data.items():setattr(old,key,val)
                old.version+=1;changed+=1
        else:
            new=Rule(team_id=tid,**data);db.add(new);changed+=1
    if manifest['disable_missing']:
        allowed={r['name'] for r in rules}
        for r in current.values():
            if r.name not in allowed and r.enabled:
                r.enabled=False;r.version+=1;changed+=1
    if changed:audit(db,'rule_manifest_published',tid,actor=actor,detail={'changed':changed})
    return {'changed':changed,'team_id':tid}


_seen:dict[str,str]={}
def reload_approved_rule_files(db_factory,folder:str|Path='config/rules'):
    folder=Path(folder)
    for path in sorted(folder.glob('*.json')):
        try:
            payload=path.read_bytes()
            sha=hashlib.sha256(payload).hexdigest()
            if _seen.get(str(path))==sha:continue
            manifest=json.loads(payload)
            with db_factory() as db:
                result=apply_signed_manifest(db,manifest)
                db.commit()
            _seen[str(path)]=sha
            log.info('Applied signed rule file %s: %s',path.name,result)
        except Exception as exc:
            log.warning('Rejected rule file %s: %s; previous active rules preserved',path.name,exc)
