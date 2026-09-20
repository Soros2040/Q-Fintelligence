"""Learned candidate scores and strict finite-catalogue LLM selection."""
from __future__ import annotations
from pathlib import Path
import numpy as np
from jsonschema import Draft202012Validator
from .api import clean, identity, load_json, write_json
from .paths import PACKAGE
from .legacy.quantum_worker.qas30_protocol import PREDICTOR_FEATURE_WHITELIST, _ridge_fit, _select_ridge_alpha

FEATURES = tuple(PREDICTOR_FEATURE_WHITELIST)
REASON_CODES = ('R1_GENERALIZATION', 'K_TOPOLOGY_FIT', 'ACTIVE_CALIBRATION_FIT',
                'COMPILE_RESOURCE_EFFICIENCY', 'LOCAL_OBJECTIVE_QUALITY', 'FROZEN_TIE_BREAK')


def validate_production_request(request):
    """Original v2 JSON Schema plus the selector's finite-set invariants."""
    schema = load_json(PACKAGE / 'schemas/qas30_ai_selection_request.schema.json')
    errors = list(Draft202012Validator(schema).iter_errors(request))
    if errors:
        raise ValueError('QAS30_PRODUCTION_REQUEST_SCHEMA:' + errors[0].message)
    ids = [c['candidateId'] for c in request['candidates']]
    bundles = [b['bundleId'] for b in request['feasibleBundles']]
    if len(set(ids)) != len(ids) or len(set(bundles)) != len(bundles):
        raise ValueError('QAS30_UNIQUE_CANDIDATE_AND_BUNDLE_IDS')
    for c in request['candidates']:
        if not all(np.isfinite(c[k]) for k in ['localValidationLoss', 'quboObjectiveDegradation', 'calibrationNoiseProxy']):
            raise ValueError('QAS30_FINITE_CANDIDATE_FEATURES')
    if any(set(b['candidateIds'])-set(ids) for b in request['feasibleBundles']):
        raise ValueError('QAS30_BUNDLE_OUTSIDE_CANDIDATE_SET')
    return request


class LearningProxy:
    """Original QAS30 standardized ridge with leave-one-candidate-out alpha."""
    def fit(self, rows, targets):
        x = self._matrix(rows)
        y = np.asarray(targets, dtype=float)
        if len(x) < 3 or y.shape != (len(x),) or not np.isfinite(y).all():
            raise ValueError('AT_LEAST_THREE_FINITE_TRAINING_CANDIDATES')
        self.alpha, self.cv_losses = _select_ridge_alpha(x, y)
        self.beta, self.intercept = _ridge_fit(x, y, self.alpha)
        self.mean, self.scale = x.mean(0), x.std(0)
        self.scale[self.scale == 0] = 1
        self.training_sha256 = identity({'rows': rows, 'targets': targets})
        self.n_train = len(x)
        return self

    @staticmethod
    def _matrix(rows):
        if not isinstance(rows, list) or not rows or any(set(row) != set(FEATURES) for row in rows):
            raise ValueError('PREDICTOR_FEATURE_WHITELIST')
        x = np.asarray([[r[k] for k in FEATURES] for r in rows], dtype=float)
        if not np.isfinite(x).all():
            raise ValueError('FINITE_PREDICTOR_FEATURES')
        return x

    def predict(self, rows):
        if not hasattr(self, 'beta'):
            raise ValueError('PROXY_NOT_FITTED')
        return self.intercept + ((self._matrix(rows)-self.mean)/self.scale) @ self.beta

    def save(self, path):
        write_json(path, {'schemaVersion': 'qf.learning-proxy.v2', 'features': FEATURES, 'alpha': self.alpha,
                         'cv_losses': self.cv_losses, 'beta': self.beta, 'intercept': self.intercept,
                         'mean': self.mean, 'scale': self.scale, 'training_sha256': self.training_sha256, 'n_train': self.n_train})

    @classmethod
    def load(cls, path):
        state = load_json(path)
        if state.get('schemaVersion') != 'qf.learning-proxy.v2' or tuple(state['features']) != FEATURES:
            raise ValueError('PROXY_STATE_SCHEMA')
        obj = cls()
        for key in ['alpha', 'cv_losses', 'beta', 'intercept', 'mean', 'scale', 'training_sha256', 'n_train']:
            setattr(obj, key, np.asarray(state[key], dtype=float) if key in {'beta', 'mean', 'scale'} else state[key])
        if not all(np.isfinite(getattr(obj, k)).all() for k in ['beta', 'mean', 'scale']) or np.any(obj.scale <= 0):
            raise ValueError('PROXY_STATE_FINITE_SCALE')
        return obj


def validate_decision(decision, request):
    required = {'schemaVersion', 'stage', 'targetStage', 'selectedBundleId', 'selectedCandidateIds', 'reasonCodes'}
    if set(decision) != required or decision['schemaVersion'] != 'qf.qas30.ai-selection-decision.v2':
        raise ValueError('LLM_DECISION_SCHEMA')
    if decision['stage'] != 'LLM_QAS_AUGMENT' or decision['targetStage'] != 'R2_LLM':
        raise ValueError('LLM_DECISION_STAGE')
    bundles = request.get('feasibleBundles', [])
    match = [b for b in bundles if b['bundleId'] == decision['selectedBundleId']]
    if len(match) != 1 or decision['selectedCandidateIds'] != match[0]['candidateIds']:
        raise ValueError('LLM_DECISION_OUTSIDE_FROZEN_BUNDLE')
    allowed = set(REASON_CODES)
    reasons = decision['reasonCodes']
    if not isinstance(reasons, list) or not reasons or len(set(reasons)) != len(reasons) or set(reasons)-allowed:
        raise ValueError('LLM_REASON_CODES')
    return clean(decision)


def build_request(candidate_ids, predictions, *, run_id, data_epoch):
    if len(candidate_ids) < 5 or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError('FIVE_UNIQUE_CANDIDATES_REQUIRED')
    values = np.asarray(predictions, dtype=float)
    if values.shape != (len(candidate_ids),) or not np.isfinite(values).all():
        raise ValueError('PREDICTION_AXIS')
    order = sorted(range(len(values)), key=lambda i: (values[i], candidate_ids[i]))
    selected = [candidate_ids[i] for i in order[:5]]
    return {'schemaVersion': 'qf.software-candidate-request.v2', 'runId': run_id, 'dataEpoch': data_epoch,
            'stage': 'LLM_QAS_AUGMENT', 'targetStage': 'R2_LLM',
            'candidateIds': list(candidate_ids), 'predictedScores': values.tolist(),
            'feasibleBundles': [{'bundleId': 'learned_top5', 'candidateIds': selected},
                                {'bundleId': 'catalogue_first5', 'candidateIds': list(candidate_ids[:5])}],
            'allowedReasonCodes': list(REASON_CODES),
            'selection_scope': 'SOFTWARE_LOCAL_CATALOGUE', 'request_sha256': identity({'ids': candidate_ids, 'scores': values.tolist(), 'run': run_id})}
