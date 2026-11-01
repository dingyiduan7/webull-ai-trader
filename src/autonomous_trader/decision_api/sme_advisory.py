"""Optional, nonbinding SME evidence. No default activation or network access.

The trusted caller supplies a pinned artifact and explicit authorization DB.
Only existing event-scoped export permission proves eligibility. This module
does not grant permission, discover files, or write the advisory database.
"""
from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3


@dataclass(frozen=True)
class ApprovedSMEAdvisory:
    path: Path
    sha256: str
    authorization_db: Path


@dataclass(frozen=True)
class RotatingSMEAdvisory:
    config_path: Path
    config_sha256: str
    provider_id: str
    authorization_db: Path
    pointer_path: Path
    max_publication_age_seconds: int


def _read_json(path,limit=4096):
    with Path(path).open('rb') as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('OVERSIZE')
    return json.loads(raw,object_pairs_hook=_unique)


def rotating_configuration(path,expected_sha256):
    """Freeze explicit local provider identity; changes require new composition."""
    try:
        with Path(path).open('rb') as stream:raw=stream.read(4097)
        if len(raw)>4096 or not isinstance(expected_sha256,str) or sha256(raw).hexdigest()!=expected_sha256:return None
        value=json.loads(raw,object_pairs_hook=_unique)
        if (value.get('enabled') is not True or not isinstance(value['provider_id'],str) or not value['provider_id']
                or type(value['max_publication_age_seconds']) is not int or value['max_publication_age_seconds']!=60):return None
        db=Path(value['authorization_db']);pointer=Path(value['pointer_path'])
        if not db.is_absolute() or not pointer.is_absolute():return None
        return RotatingSMEAdvisory(Path(path).resolve(),_digest({k:v for k,v in value.items() if k!='enabled'}),
            value['provider_id'],db.resolve(),pointer.resolve(),value['max_publication_age_seconds'])
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError):return None


def _resolve_rotating(provider,now):
    # Configuration may revoke availability, never silently change authority.
    config=_read_json(provider.config_path)
    if (config.get('enabled') is not True
            or _digest({k:v for k,v in config.items() if k!='enabled'})!=provider.config_sha256):
        return None,'IGNORED_PROVIDER_DISABLED_OR_CHANGED',None
    pointer=_read_json(provider.pointer_path)
    if pointer.get('schema')!='TRADER_SME_PROVIDER_V1' or pointer.get('provider_id')!=provider.provider_id:
        return None,'IGNORED_PROVIDER_IDENTITY',None
    published=datetime.fromisoformat(pointer['published_at'].replace('Z','+00:00'))
    age=(now-published).total_seconds()
    if published.tzinfo is None or not 0<=age<=provider.max_publication_age_seconds:
        return None,'IGNORED_PROVIDER_PUBLICATION_STALE',None
    if pointer.get('status')!='AVAILABLE':return None,'IGNORED_PROVIDER_UNAVAILABLE',None
    artifact=Path(pointer['artifact_path'])
    if not artifact.is_absolute() or artifact.resolve().parent!=provider.pointer_path.parent or artifact.is_symlink():
        return None,'IGNORED_PROVIDER_ARTIFACT_PATH',None
    return ApprovedSMEAdvisory(artifact,pointer['sha256'],provider.authorization_db),'RESOLVED',{
        'provider_id':provider.provider_id,'published_at':published.isoformat(),'publication_age_seconds':age,
        'publication_freshness':'CURRENT_PUBLICATION_NOT_PROOF_OF_CURRENT_MARKET_APPLICABILITY'}


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate')
        result[key] = value
    return result


def _verification(value):
    if isinstance(value, dict):
        return (value.get('verification_only') is True
                or str(value.get('source', '')).lower().startswith('synthetic')
                or str(value.get('classification', '')).upper().startswith('SYNTHETIC')
                or any(_verification(v) for v in value.values()))
    return isinstance(value, list) and any(_verification(v) for v in value)


def _permission(connection, record):
    row = connection.execute('''SELECT e.role,e.status,e.payload,o.body,k.body,k.sha256,k.source,o.created
        FROM events e JOIN outbox o ON o.event_id=e.id
        JOIN knowledge k ON k.id=? JOIN export_permissions p ON p.knowledge_id=k.id
        WHERE e.id=? AND o.id=? AND o.destination='astra_advisory' ''',
        (record['knowledge_id'], record['source_event'], record['outbox_id'])).fetchone()
    if not row or row[0] != 'trader' or row[1] != 'COMPLETED': return False
    payload, advice, knowledge = (json.loads(row[i], object_pairs_hook=_unique) for i in (2,3,4))
    return (payload.get('knowledge_id') == record['knowledge_id'] and not _verification(payload)
            and not _verification(knowledge) and _digest(knowledge) == row[5]
            and not str(row[6]).lower().startswith('synthetic') and row[7] == record['created_at']
            and _digest(advice) == record['sha256'] and advice == record['advice'])


def attach_approved_advisory(request, context, approved=None, *, now=None):
    """Return original request on absent/broken advice; never a trading gate."""
    if approved is None:
        return request, 'DISABLED'
    publication=None
    if isinstance(approved,RotatingSMEAdvisory):
        try:
            approved,status,publication=_resolve_rotating(approved,now or datetime.now(timezone.utc))
            if approved is None:return request,status
        except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):
            return request,'IGNORED_INVALID_OR_UNAVAILABLE'
    if (not isinstance(approved, ApprovedSMEAdvisory)
            or not isinstance(approved.sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', approved.sha256)
            or not isinstance(approved.authorization_db, (str, Path))):
        return request, 'IGNORED_UNAPPROVED'
    try:
        with Path(approved.path).open('rb') as stream:
            raw = stream.read(32769)
        if len(raw) > 32768 or sha256(raw).hexdigest() != approved.sha256:
            return request, 'IGNORED_ARTIFACT_BINDING'
        value = json.loads(raw, object_pairs_hook=_unique)
        if (set(value) != {'schema', 'authority', 'verification_only', 'records'}
                or value['schema'] != 'TRADER_SME_ADVISORY_V1'
                or value['authority'] != 'NONBINDING_ADVICE_ONLY'
                or value['verification_only'] is not False
                or not isinstance(value['records'], list) or not 1 <= len(value['records']) <= 3):
            return request, 'IGNORED_ADVISORY_CLASSIFICATION'
        records = []
        for record in value['records']:
            if (record['verification_only'] is not False
                    or not isinstance(record['source_event'], str) or not record['source_event']
                    or record['source_event'].lower().startswith('synthetic')
                    or not isinstance(record['outbox_id'], str) or not record['outbox_id']
                    or record['advice']['operation'] != 'advise'
                    or record['sha256'] != _digest(record['advice'])):
                return request, 'IGNORED_ADVISORY_PROVENANCE'
            created = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
            if created.tzinfo is None or created > context.as_of:
                return request, 'IGNORED_ADVISORY_TIME'
            with closing(sqlite3.connect(Path(approved.authorization_db).resolve().as_uri()+'?mode=ro', uri=True, timeout=1)) as connection:
                if not _permission(connection, record):
                    return request, 'IGNORED_EXPORT_PERMISSION'
            records.append({'source': 'trader_sme', 'source_event': record['source_event'],
                'outbox_id': record['outbox_id'], 'knowledge_id': record['knowledge_id'], 'assessment_sha256': record['sha256'],
                'assessment_created_at': created.astimezone(timezone.utc).isoformat(),
                'context_as_of': context.as_of.isoformat(),
                'assessment_age_seconds': (context.as_of-created).total_seconds(),
                'temporal_status': 'VALIDITY_UNKNOWN', 'market_evidence_as_of': None,
                'unknowns': ['Underlying market observation time and current applicability are not verified.'],
                'verification_only': False, 'classification': 'UNTRUSTED_MODEL_ASSESSMENT_NOT_VERIFIED_FACT',
                'authority': 'NONBINDING_ADVICE_ONLY', 'assessment': record['advice']})
        attached = deepcopy(request)
        payload = json.loads(attached['input'][1]['content'])
        payload['trader_sme_advisory'] = {
            'authority': 'NONBINDING_ADVICE_ONLY', 'artifact_sha256': approved.sha256,
            'astra_discretion': 'Optional untrusted evidence; Astra may reject it. No override of current market evidence or hard policy.',
            'records': records}
        if publication:payload['trader_sme_advisory']['publication']=publication
        attached['input'][1]['content'] = json.dumps(payload)
        return attached, 'ATTACHED_APPROVED_NONBINDING'
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, sqlite3.Error):
        return request, 'IGNORED_INVALID_OR_UNAVAILABLE'
