"""Durable adapter for governed LLM calls and original Tianyan176 contracts."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable
from .api import digest, identity, load_json
from .legacy.research_common import write_json as durable_json
from .legacy.quantum_worker import qas30_experiment as experiment
from .legacy.quantum_worker import qas30_tianyan as tianyan
from .selection import validate_decision, validate_production_request


class GovernedLLMSelector:
    """Call an injected authorized provider once, after durable admission.

    envelope_verifier must invoke the host control plane's AuthorizationEnvelope/
    committing-dispatch verification, including active lease and fencing token.
    The package does not mint permission or accept a boolean authorization flag.
    invoke receives the request and the same verified envelope, and returns
    decision, providerRequestId and usage. The upstream TS provider can be bridged
    through this callable. Unknown outcomes retain a sticky on-disk stop state.
    """
    def __init__(self, *, provider: str, model: str, invoke: Callable, envelope_verifier: Callable):
        if provider not in {'DEEPSEEK', 'MINIMAX'} or not isinstance(model, str) or not model:
            raise ValueError('SUPPORTED_PROVIDER_AND_MODEL_REQUIRED')
        if not callable(invoke) or not callable(envelope_verifier):
            raise ValueError('GOVERNED_TRANSPORT_AND_ENVELOPE_VERIFIER_REQUIRED')
        self.provider, self.model, self.invoke, self.verify_envelope = provider, model, invoke, envelope_verifier

    def select(self, request, *, data_epoch, authorization_envelope, resource_snapshot, output_dir, now=None):
        current = now or datetime.now(timezone.utc)
        validate_production_request(request)
        if not isinstance(data_epoch, str) or not data_epoch:
            raise ValueError('FROZEN_DATA_EPOCH_REQUIRED')
        experiment.require_fresh_green_snapshot(resource_snapshot, now=current)
        # The delegated verifier MUST fail/raise for forged, expired or stale leases.
        binding = self.verify_envelope(authorization_envelope, request, self.provider, self.model)
        expected = {'runId': request['runId'], 'dataEpoch': data_epoch, 'requestSha256': identity(request),
                    'provider': self.provider, 'model': self.model}
        if not isinstance(binding, dict) or any(binding.get(k) != v for k, v in expected.items()):
            raise ValueError('AUTHORIZATION_ENVELOPE_BINDING_MISMATCH')
        if not binding.get('actionId') or not binding.get('attemptId'):
            raise ValueError('AUTHORIZATION_ACTION_AND_ATTEMPT_REQUIRED')
        out = Path(output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        # mkdir is the cross-process, crash-sticky exactly-once claim. No retries.
        claim = out / ('attempt_' + identity({'action': binding['actionId'], 'attempt': binding['attemptId']}))
        claim.mkdir(exist_ok=False)
        safe_binding = {k: binding[k] for k in (*expected, 'actionId', 'attemptId')}
        durable_json(claim / 'intent.json', {'schemaVersion': 'qf.governed-llm-intent.v2', **safe_binding,
                                          'authorizationEnvelopeSha256': identity(authorization_envelope),
                                          'resourceSnapshotSha256': identity(resource_snapshot), 'state': 'PREPARED'})
        try:
            # Recheck freshness at the actual dispatch boundary after fsync.
            experiment.require_fresh_green_snapshot(resource_snapshot, now=now or datetime.now(timezone.utc))
            durable_json(claim / 'state.json', {'state': 'DISPATCHING'})
            response = self.invoke(request, authorization_envelope)
            if not isinstance(response, dict) or not response.get('providerRequestId') or not isinstance(response.get('usage'), dict):
                raise ValueError('PROVIDER_REQUEST_ID_AND_USAGE_REQUIRED')
            decision = validate_decision(response['decision'], request)
            request_id = str(response['providerRequestId'])
            receipt = {'schemaVersion': 'qf.governed-llm-receipt.v2', **safe_binding,
                       'providerRequestId': request_id, 'usage': response['usage'], 'decision': decision,
                       'decisionSha256': identity(decision), 'state': 'COMPLETE', 'external_calls': 1}
            if len(json.dumps(receipt).encode()) > 262144:
                raise ValueError('BOUNDED_RECEIPT_REQUIRED')
            # A provider request ID may appear only once across this receipt ledger.
            unique = out / ('request_' + identity(request_id))
            unique.mkdir(exist_ok=False)
            durable_json(claim / 'receipt.json', receipt)
            durable_json(claim / 'state.json', {'state': 'COMPLETE', 'receiptSha256': digest(claim / 'receipt.json')}, replace=True)
            return receipt
        except Exception:
            durable_json(claim / 'state.json', {'state': 'STICKY_STOP_UNKNOWN_OR_UNUSABLE_OUTCOME', 'replayAllowed': False}, replace=True)
            raise


class Tianyan176Adapter:
    """Original QAS30 implementation, with unchanged per-action governance."""
    validate_live_batch = staticmethod(tianyan.validate_live_batch)
    require_authorization = staticmethod(experiment.require_execution_authorization)
    require_resource_snapshot = staticmethod(experiment.require_fresh_green_snapshot)
    login_once = staticmethod(experiment.governed_login_once)
    submit_next = staticmethod(experiment.governed_submit_next)
    query_due = staticmethod(experiment.governed_query_due)
    reconcile = staticmethod(experiment.reconcile_local)
    validate_query_observation = staticmethod(tianyan.validate_query_observation_against_batch)


def audit_receipt(path, *, batch_path=None, submit_receipt_path=None):
    path = Path(path)
    receipt = load_json(path)
    if batch_path is not None:
        if submit_receipt_path is None:
            raise ValueError('SUBMIT_RECEIPT_REQUIRED_FOR_QUERY_REVALIDATION')
        batch = load_json(batch_path)
        tianyan.validate_live_batch(batch)
        tianyan.validate_query_observation_against_batch(receipt, live_batch=batch, submit_receipt=load_json(submit_receipt_path))
        checks = ['LIVE_BATCH_SCHEMA', 'QUERY_OBSERVATION_BOUND_TO_BATCH']
    else:
        if not isinstance(receipt, dict) or not receipt.get('schemaVersion'):
            raise ValueError('VERSIONED_RECEIPT_REQUIRED')
        checks = ['VERSION_PRESENT', 'CONTENT_SHA256']
    return {'status': 'PASS', 'schemaVersion': receipt['schemaVersion'], 'sha256': digest(path), 'checks': checks,
            'external_calls': 0, 'scientificStatus': 'LOCAL_VERIFIED' if batch_path else 'STRUCTURAL_INSPECTION_ONLY'}
