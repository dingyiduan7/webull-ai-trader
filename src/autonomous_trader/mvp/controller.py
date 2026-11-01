"""Thin M2 facade. Admission and permit consumption remain PostgreSQL transactions."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from ..durable.models import Mark, RegisterInstruments, Reconcile, Admit, ConsumePermit, Incident


class Controller:
    def __init__(self, ledger, account_id, clock=lambda:datetime.now(timezone.utc)):
        self.ledger,self.account_id,self.clock=ledger,account_id,clock
        self.holder='mvp-'+uuid4().hex
        self.token=None

    @property
    def state(self):return self.ledger.snapshot(self.account_id)

    def lease(self):
        state=self.ledger.acquire_leader(self.account_id,self.holder,event_id=uuid4().hex,at=self.clock(),ttl_seconds=60)
        self.token=state['leader']['token']
        return state

    def send(self, command):
        state=self.state
        return self.ledger.execute(self.account_id,command,event_id=uuid4().hex,expected_version=state['version'],
            holder=self.holder,token=self.token,at=self.clock())

    def _refresh(self,observation,*,exit_observation=False,resolved_incident_ids=()):
        lease_state=self.lease()
        lease_deadline=datetime.fromisoformat(lease_state['leader']['expires_at'])-timedelta(seconds=20)
        def keep_refresh_lease():
            nonlocal lease_deadline
            if self.clock()>=lease_deadline:
                renewed=self.lease()
                lease_deadline=datetime.fromisoformat(renewed['leader']['expires_at'])-timedelta(seconds=20)
        items=getattr(observation,'candidates',(observation,))
        additions=tuple(x.instrument for x in items if x.instrument.instrument_id not in self.state['instruments'])
        if additions:
            keep_refresh_lease()
            self.send(RegisterInstruments(instruments=additions,evidence_id=observation.evidence_id))
        for item in items:
            old=self.state['marks'].get(item.instrument_id)
            if (old is not None
                    and item.quote_at<datetime.fromisoformat(old['observed_at'])):
                # Concurrent broad HTTP and focused/MQTT observations can arrive
                # out of source order. Keep the newer durable mark; reconcile
                # account facts normally. Admission still requires fresh data.
                continue
            keep_refresh_lease()
            self.send(Mark(instrument_id=item.instrument_id,bid_usd=item.bid,
                observed_at=item.quote_at,source_id='WEBULL_SANDBOX'))
        keep_refresh_lease()
        return self.send(Reconcile(settled_usd=observation.settled,unsettled_usd=observation.unsettled,
            positions=tuple(observation.positions.items()),outstanding_intent_ids=tuple(observation.open_intents),
            evidence_id=observation.evidence_id,settlement_known=True,resolved_incident_ids=tuple(resolved_incident_ids),
            reconciliation_protocol="CANCEL_RECOVERY_V1"))

    def refresh(self,observation):return self._refresh(observation)

    def refresh_for_exit(self,observation):
        """Reconcile a held position without replacing a newer durable mark."""
        return self._refresh(observation,exit_observation=True)

    def resolve_recovered_cancellation_incidents(self,observation,recovered_intent_ids):
        """Clear only exact cancel-pending mismatch incidents after a clean refresh."""
        state=self.state;recovered=set(recovered_intent_ids)
        if not recovered:return state
        if not state['reconciliation']['ready'] or not state['reconciliation']['settlement_known']:
            raise ValueError('CANCEL_RECOVERY_REQUIRES_CLEAN_RECONCILIATION')
        resolved=tuple(key for key,value in state['incidents'].items()
            if not value['pending_correction'] and value.get('scope')=='CANCEL_PENDING_OUTSTANDING_ONLY'
            and value.get('intent_ids') and set(value['intent_ids'])<=recovered)
        return state if not resolved else self._refresh(observation,resolved_incident_ids=resolved)

    def admit(self,intent,plan,*,policy_hash=None):
        fee=str(1+intent.quantity*__import__('decimal').Decimal('0.02'))
        holding=getattr(plan,'max_holding_seconds',None)
        if holding is None:holding=max(1,int((plan.latest_exit_at-self.clock()).total_seconds()))
        state=self.send(Admit(intent=intent,policy_hash=policy_hash,fee_allowance_usd=fee,exit_cost_usd=fee,
            stress_allowance_usd=str(intent.quantity*intent.maximum_entry_usd*__import__('decimal').Decimal('0.001')),
            latest_exit_at=plan.latest_exit_at,max_holding_seconds=holding))
        return state['outbox'][intent.intent_id]['nonce']

    def consume(self,intent,nonce):
        before=self.state
        if before['orders'][intent.intent_id]['intent']!=intent.model_dump(mode='json'):
            raise ValueError('INTENT_BINDING_MISMATCH')
        return self.send(ConsumePermit(nonce=nonce))

    def incident(self,reason):
        return self.send(Incident(reason=reason,evidence_id=uuid4().hex))
