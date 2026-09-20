"""One executable data-to-graph-to-portfolio-to-circuit method chain."""
from __future__ import annotations
import datetime as dt
from pathlib import Path
import re
import time
import numpy as np
import pandas as pd
from .api import clean, digest, environment, identity, load_json, write_json
from .data import load_panel, validate_panel
from .paths import DataLayout, PACKAGE
from .legacy.stock_task import graph_and_risk
from .legacy.graph_head import GraphHead
from .legacy.quantum_features import catalog, states, observables
from .legacy.bridge import qubo, ising, exhaustive, solve
from .quantum import (candidate_catalog, noisy_probabilities, noise_catalog, circuit_resources,
                      energies, count_metrics, evaluate_candidate, portfolio_solution)
from .config import RunConfig
from .selection import LearningProxy, build_request, validate_decision
from .risk import fit_specific_risk, predict_specific_risk


def run_pipeline(input_data=None, *, data_root=None, frozen=False, output_dir=None, run_id=None, seed=2026090902, shots=1024, epochs=40, risk_mode='factor', risk_lambda=1., cost=.001, qaoa_gamma=.6, qaoa_beta=.25, qaoa_mode='optimize'):
    RunConfig(input_data=input_data, frozen=frozen, seed=seed, shots=shots, epochs=epochs, risk_mode=risk_mode,
              risk_lambda=risk_lambda, cost=cost, qaoa_gamma=qaoa_gamma, qaoa_beta=qaoa_beta, qaoa_mode=qaoa_mode).validate()
    if type(seed) is not int or not 0 <= seed < 2**32 or type(shots) is not int or not 1 <= shots <= 100000:
        raise ValueError('SEED_OR_SHOTS_RANGE')
    if type(epochs) is not int or not 1 <= epochs <= 200:
        raise ValueError('EPOCHS_RANGE_1_200')
    if risk_mode not in {'factor', 'shrinkage', 'quantum_specific'}:
        raise ValueError('RISK_MODE')
    run_id = run_id or dt.datetime.now(dt.timezone.utc).strftime('method_%Y%m%dT%H%M%S_%fZ')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', run_id):
        raise ValueError('RUN_ID_SCHEMA')
    out = Path(output_dir or Path.cwd() / 'outputs').resolve() / run_id
    if out.is_relative_to(PACKAGE):
        raise ValueError('OUTPUT_INSIDE_IMPLEMENTATION')
    if data_root is not None and DataLayout.resolve(data_root).protected(out):
        raise ValueError('OUTPUT_INSIDE_FROZEN_DATA')
    document = load_panel(input_data, frozen=frozen, data_root=data_root, seed=seed)
    a = validate_panel(document)
    out.mkdir(parents=True, exist_ok=False)
    tick = time.process_time()
    write_json(out / 'input.json', document)
    config = {'run_id': run_id, 'data_epoch': document['data_epoch'], 'seed': seed, 'shots': shots,
              'epochs': epochs, 'risk_mode': risk_mode, 'risk_lambda': risk_lambda, 'cost': cost,
              'qaoa_gamma': qaoa_gamma, 'qaoa_beta': qaoa_beta, 'qaoa_mode': qaoa_mode,
              'input_sha256': digest(out / 'input.json'), 'external_calls': 0,
              'implementation_sha256': identity({str(p.relative_to(PACKAGE)): digest(p) for p in PACKAGE.rglob('*.py')})}
    config['request_sha256'] = identity(config)
    write_json(out / 'intent.json', config)
    try:
        dates = document['dates']
        indices = np.arange(125, len(dates)-2)
        evaluation_start = indices[max(8, int(len(indices)*.75))]
        # A signal's target spans the next open through the following open.
        train = np.array([i for i in indices if i < evaluation_start and document['label_available_dates'][i] < dates[evaluation_start]])
        evaluation = indices[indices >= evaluation_start]
        if len(train) < 8 or len(evaluation) < 2:
            raise ValueError('INSUFFICIENT_PURGED_TRAIN_EVALUATION_DATES')
        all_indices = np.concatenate([train, evaluation])
        graphs, sigma_list, fevd_list, factors, shrinkages, history_means = [], [], [], [], [], []
        for t in all_indices:
            directed, _, fevd, loading, factor, specific, sigma, shrinkage, stabilization = graph_and_risk(a['close_returns'][t-125:t+1], a['open_returns'][t-125:t+1])
            graphs.append(.5*np.eye(6)+.5*directed)
            sigma_list.append(sigma); fevd_list.append(fevd)
            shrinkages.append(shrinkage); history_means.append(a['open_returns'][t-125:t+1].mean(0))
            factors.append({'Lambda': loading, 'F': factor, 'd': specific, 'VAR_stabilization': stabilization})
        graph = np.asarray(graphs)
        x = graph @ a['features'][all_indices]
        ntrain = len(train)
        mean, scale = x[:ntrain].mean((0, 1)), np.maximum(x[:ntrain].std((0, 1)), 1e-8)
        angles = np.clip((x-mean)/scale, -3, 3)*np.pi/3
        np.savez_compressed(out / 'feature_trace.npz', aggregated_features=x, train_mean=mean,
                            train_scale=scale, angles=angles, all_indices=all_indices,
                            train_indices=train, evaluation_indices=evaluation)
        state = states(angles.reshape(-1, 6), catalog()[0])
        readout = observables(state).reshape(len(all_indices), 6, 11)
        y, mask = a['labels'][all_indices], a['label_valid'][all_indices]
        head = GraphHead(11, 16, 1, .001, seed)
        loss = head.fit(readout[:ntrain], graph[:ntrain], y[:ntrain], mask[:ntrain], epochs=epochs)
        prediction = head.predict(readout, graph)
        residual = y[:ntrain]-prediction[:ntrain]
        sigma = sigma_list[-1] if risk_mode == 'factor' else shrinkages[-1]
        predicted_d = None
        if risk_mode == 'quantum_specific':
            fit = fit_specific_risk(readout[:ntrain], y[:ntrain], history_means[:ntrain],
                                    [f['Lambda'] for f in factors[:ntrain]], mask[:ntrain], seed=seed)
            predicted_d = predict_specific_risk(fit, readout[-1])
            sigma = factors[-1]['Lambda']@factors[-1]['F']@factors[-1]['Lambda'].T+np.diag(predicted_d)
            model, scaler, target_scale = fit
            np.savez_compressed(out / 'specific_risk_model.npz', coef=model.coef_, intercept=model.intercept_,
                                xmean=scaler.mean_, xscale=scaler.scale_, target_scale=np.array(target_scale))
        if np.linalg.eigvalsh(sigma).min() < -1e-10:
            raise ValueError('RISK_PSD')
        np.savez_compressed(out / 'mean_model.npz', **head.export())
        np.savez_compressed(out / 'risk_arrays.npz', graphs=graph, fevd=fevd_list, readout=readout, covariance=sigma_list, selected_Sigma=sigma)
        risk = {'schemaVersion': 'qf.risk-artifact.v2', 'asset_order': document['asset_order'], 'signal_date': dates[int(all_indices[-1])],
                'mu': prediction[-1], 'predicted_specific_variance': predicted_d, 'Sigma': sigma, 'factor_decomposition': factors[-1],
                'risk_mode': risk_mode, 'risk_unit': 'ONE_SESSION_SIMPLE_RETURN_VARIANCE',
                'return_unit': 'SIMPLE_FRACTION', 'horizon': 'NEXT_OPEN_TO_FOLLOWING_OPEN', 'window_sessions': 126,
                'train_dates': [dates[int(i)] for i in train], 'evaluation_dates': [dates[int(i)] for i in evaluation],
                'latest_training_label_available': max(document['label_available_dates'][int(i)] for i in train),
                'scaler_fit': 'TRAIN_ONLY', 'input_sha256': config['input_sha256']}
        write_json(out / 'risk_artifact.json', risk)
        instance = {'instance_id': run_id, 'asset_order': document['asset_order'], 'mu': prediction[-1].tolist(), 'sigma': sigma.tolist(),
                    'previous': [1/3, 1/3, 1/3, 0, 0, 0], 'k': 3, 'lambda': risk_lambda, 'cost': cost}
        write_json(out / 'bridge_instance.json', instance)
        matrix, values, penalized, optimum, penalty = energies(instance)
        q, constant = qubo(np.array(instance['mu']), sigma, 3, risk_lambda, cost, np.array(instance['previous']), penalty=penalty)
        fields, couplings, shift = ising(q, constant)
        bits = ((np.arange(64)[:, None] >> np.arange(6)) & 1).astype(float)
        z = 1-2*bits
        qubo_energy = np.einsum('bi,ij,bj->b', bits, q, bits)+constant
        ising_energy = z@fields+np.einsum('bi,ij,bj->b', z, couplings, z)+shift
        error = max(np.abs(qubo_energy-penalized).max(), np.abs(ising_energy-penalized).max())
        if error > 1e-10:
            raise ValueError('BRIDGE_REPRESENTATION_IDENTITY')
        best, basket = exhaustive(np.array(instance['mu']), sigma, 3, risk_lambda, cost, np.array(instance['previous']))
        chosen, classical = solve(np.array(instance['mu']), sigma, 3, risk_lambda, cost, np.array(instance['previous']), cpu_cap=1)
        write_json(out / 'portfolio_solution.json', portfolio_solution(instance, chosen, basket))
        write_json(out / 'qubo.json', {'Q': q, 'offset': constant, 'h': fields, 'J': couplings, 'ising_offset': shift,
                                     'penalty': penalty, 'maximum_identity_error': float(error), 'bit_order': 'q5...q0'})
        candidates, feature_rows, targets, records = candidate_catalog()[:6], [], [], []
        ideal_distributions, noisy_distributions, traces = [], [], []
        rng = np.random.default_rng(seed)
        for candidate in candidates:
            qcis, ideal, trace = evaluate_candidate(instance, candidate, penalized, qaoa_gamma, qaoa_beta, qaoa_mode)
            traces.append(trace)
            (out / (candidate['candidate_id'] + '.qcis')).write_text(qcis, encoding='utf-8')
            noisy = noisy_probabilities(qcis, noise_catalog()[1])
            ideal_distributions.append(ideal); noisy_distributions.append(noisy)
            resource = circuit_resources(qcis)
            counts = rng.multinomial(shots, noisy)
            metrics = count_metrics({f'{i:06b}': int(n) for i, n in enumerate(counts)}, values, penalized, optimum)
            feature_rows.append({'localValidationLoss': float(np.abs(residual)[mask[:ntrain]].mean()),
                                 'quboObjectiveDegradation': float(ideal@penalized-optimum),
                                 'compiledDepth': resource['logical_depth'], 'twoQubitGates': resource['two_qubit_gates'],
                                 'swapCount': resource['swap_count'],
                                 'calibrationNoiseProxy': .001*resource['gate_count']+.005*resource['two_qubit_gates']})
            targets.append(float(noisy@penalized-optimum))
            records.append({'candidate': candidate['candidate_id'], 'parameters': trace['parameters'], 'qaoa_mode': qaoa_mode,
                            'optimizer_evaluations': trace['nfev'], 'optimizer_success': trace['success'],
                            'optimizer_status': trace['status'], 'optimizer_message': trace['message'],
                            'ideal_expected_gap': float(ideal@penalized-optimum), 'density_expected_gap': targets[-1],
                            'counts': {f'{i:06b}': int(n) for i, n in enumerate(counts)}, **metrics, **resource})
        proxy = LearningProxy().fit(feature_rows[:4], targets[:4])
        scores = proxy.predict(feature_rows)
        proxy.save(out / 'learning_proxy.json')
        request = build_request([c['candidate_id'] for c in candidates], scores, run_id=run_id, data_epoch=document['data_epoch'])
        request['configuration'] = {k: config[k] for k in ['risk_lambda', 'cost', 'qaoa_gamma', 'qaoa_beta', 'qaoa_mode']}
        request['implementation_sha256'] = config['implementation_sha256']
        request['request_sha256'] = identity({k: v for k, v in request.items() if k != 'request_sha256'})
        write_json(out / 'candidate_request.json', request)
        selected_bundle = request['feasibleBundles'][0]
        # The local path explicitly records a learned decision, never a provider outcome.
        decision = {'schemaVersion': 'qf.qas30.ai-selection-decision.v2', 'stage': 'LLM_QAS_AUGMENT', 'targetStage': 'R2_LLM',
                    'selectedBundleId': selected_bundle['bundleId'], 'selectedCandidateIds': selected_bundle['candidateIds'],
                    'reasonCodes': ['LOCAL_OBJECTIVE_QUALITY']}
        validate_decision(decision, request)
        write_json(out / 'selection.json', {'origin': 'LOCAL_LEARNED_PROXY', 'provider_calls': 0,
                                          'decision': decision, 'training_candidates': 4, 'heldout_candidates': 2,
                                          'heldout_mae': float(np.abs(scores[4:]-targets[4:]).mean())})
        write_json(out / 'quantum_results.json', records)
        np.savez_compressed(out / 'quantum_probabilities.npz', candidate_names=[c['candidate_id'] for c in candidates],
                            ideal=ideal_distributions, noisy=noisy_distributions)
        write_json(out / 'optimization_trace.json', {'schemaVersion': 'qf.optimization-trace.v1',
                   'mode': qaoa_mode, 'initial_parameters': [qaoa_gamma, qaoa_beta],
                   'maxiter': 18 if qaoa_mode == 'optimize' else 0, 'rhobeg': .35 if qaoa_mode == 'optimize' else None,
                   'candidates': traces})
        order = sorted(range(len(candidates)), key=lambda i: (scores[i], candidates[i]['candidate_id']))
        ranks = {i: rank+1 for rank, i in enumerate(order)}
        pd.DataFrame([{'candidate': c['candidate_id'], **feature_rows[i], 'observed_target': targets[i],
                       'prediction': float(scores[i]), 'split': 'train' if i < 4 else 'heldout',
                       'rank': ranks[i], 'selected': c['candidate_id'] in selected_bundle['candidateIds']}
                      for i, c in enumerate(candidates)]).to_csv(out / 'candidate_audit.csv', index=False)
        pd.DataFrame({'date': np.repeat([dates[int(i)] for i in evaluation], 6),
                      'asset': document['asset_order']*len(evaluation), 'target': y[ntrain:].ravel(),
                      'prediction': prediction[ntrain:].ravel()}).to_csv(out / 'predictions.csv', index=False)
        write_json(out / 'training_loss.json', loss)
        summary = {'status': 'COMPLETE', 'assets': 6, 'train_dates': len(train), 'evaluation_dates': len(evaluation),
                   'sdk_feature_evolutions': len(state), 'risk_minimum_eigenvalue': float(np.linalg.eigvalsh(sigma).min()),
                   'quantum_feature_head_mae': float(np.abs(prediction[ntrain:]-y[ntrain:])[mask[ntrain:]].mean()),
                   'classical': classical, 'exact_objective': best, 'exact_basket': list(basket),
                   'qubo_ising_max_error': float(error), 'candidates': len(candidates), 'shots': len(candidates)*shots,
                   'selected_candidates': selected_bundle['candidateIds'], 'external_calls': 0}
        artifacts = [{'path': p.name, 'sha256': digest(p), 'bytes': p.stat().st_size} for p in sorted(out.iterdir()) if p.is_file()]
        baseline = load_json(PACKAGE / 'provenance/delivery_baseline.json')
        result = {'schemaVersion': 'qf.software-result.v2', 'runId': run_id, 'dataEpoch': document['data_epoch'],
                  'experimentId': 'SOFTWARE_METHOD_CHAIN', 'sourceCommit': baseline['workspaceCommit'], 'workspaceCommit': baseline['workspaceCommit'],
                  'executionMode': 'LOCAL_FROZEN_INPUT_INTEGRATION' if frozen else 'LOCAL_NORMALIZED_INPUT',
                  'scientificStatus': 'LOCAL_VERIFIED', 'archiveStatus': 'RESULT_GENERATED', 'releaseStatus': 'LOCAL_REVIEW',
                  'scientific_scope': 'SOFTWARE_INTEGRATION_CASE; historical experiment conclusions remain in their original registry',
                  'summary': summary, 'configuration': config, 'parents': document['parents'], 'artifacts': artifacts,
                  'implementation': {str(p.relative_to(PACKAGE)): digest(p) for p in PACKAGE.rglob('*.py')},
                  'environment': environment(), 'cpu_seconds': time.process_time()-tick}
        write_json(out / 'result.json', result)
        (out / 'result.sha256').write_text(digest(out / 'result.json')+'  result.json\n')
        return {'output_dir': str(out), **clean(result)}
    except Exception as exc:
        write_json(out / 'failure.json', {'status': 'FAILED', 'error_type': type(exc).__name__, 'message': str(exc)})
        raise
