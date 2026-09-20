"""Parameterized six-asset portfolio kernel and observed QAOA evaluation trace."""
from __future__ import annotations
import itertools
import numpy as np
from scipy.optimize import minimize
from .config import validate_compute_parameters
from .legacy.bridge import objective, qubo
from .legacy.quantum_search import (BIT_MATRIX, FEASIBLE, candidate_catalog, count_metrics,
                                  circuit_resources, noise_catalog, noisy_probabilities,
                                  rzz, sdk_probabilities)


def energies(instance):
    mu, sigma, previous = map(np.asarray, [instance['mu'], instance['sigma'], instance['previous']])
    k, risk_lambda, cost = instance['k'], instance['lambda'], instance['cost']
    validate_compute_parameters(risk_lambda, cost, .6, .25, 'fixed')
    if type(k) is not int or k != 3 or mu.shape != (6,) or sigma.shape != (6, 6) or previous.shape != (6,):
        raise ValueError('SIX_ASSET_SELECT_THREE_INSTANCE')
    if not all(np.isfinite(a).all() for a in (mu, sigma, previous)):
        raise ValueError('FINITE_PORTFOLIO_INSTANCE')
    if not np.array_equal(previous, np.array([1/3, 1/3, 1/3, 0., 0., 0.])):
        raise ValueError('INITIAL_EQUAL_WEIGHT_FIRST_THREE')
    matrix, const = qubo(mu, sigma, k, risk_lambda, cost, previous)
    if not np.allclose(matrix, matrix.T, atol=1e-12):
        raise ValueError('SYMMETRIC_QUBO')
    values = np.einsum('bi,ij,bj->b', BIT_MATRIX, matrix, BIT_MATRIX)+const
    direct = np.array([objective(x, mu, sigma, k, risk_lambda, cost, previous) for x in BIT_MATRIX])
    if not np.allclose(values, direct, rtol=1e-10, atol=1e-12):
        raise ValueError('BRIDGE_IDENTITY')
    bound = float(np.abs(np.diag(matrix)).sum()+2*np.abs(np.triu(matrix, 1)).sum())
    penalty = 1.01*bound+1e-12
    penalized = values+penalty*(BIT_MATRIX.sum(axis=1)-k)**2
    optimum = float(values[FEASIBLE].min())
    return matrix, values, penalized, optimum, penalty


# Public descriptive spelling shared by numerical audit and teaching views.
portfolio_energies = energies


def qaoa_qcis(instance, spec, parameters):
    """Compile ordered XY mixers using the effective portfolio coefficients."""
    parameters = np.asarray(parameters, float)
    if parameters.shape != (2*spec['depth'],) or not np.isfinite(parameters).all():
        raise ValueError('QAOA_PARAMETER_SHAPE')
    matrix, values, _, _, _ = energies(instance)
    scale = max(float(np.ptp(values[FEASIBLE])), 1e-12)
    off = matrix.copy(); np.fill_diagonal(off, 0)
    h = -.5*np.diag(matrix)-.5*off.sum(axis=1)
    lines = [f'X Q{q}' for q in spec['initial_ones']]
    for layer in range(spec['depth']):
        gamma, beta = parameters[2*layer:2*layer+2]
        for q in range(6):
            lines.append(f'RZ Q{q} {float(2*gamma*h[q]/scale):.17g}')
        for i, j in itertools.combinations(range(6), 2):
            if abs(matrix[i, j]) > 0:
                rzz(lines, i, j, gamma*matrix[i, j]/scale)
        for i, j in spec['mixer_edges']:
            lines.extend([f'H Q{i}', f'H Q{j}'])
            rzz(lines, i, j, beta)
            lines.extend([f'H Q{i}', f'H Q{j}', f'RZ Q{i} {-np.pi/2:.17g}',
                          f'RZ Q{j} {-np.pi/2:.17g}', f'H Q{i}', f'H Q{j}'])
            rzz(lines, i, j, beta)
            lines.extend([f'H Q{i}', f'H Q{j}', f'RZ Q{i} {np.pi/2:.17g}', f'RZ Q{j} {np.pi/2:.17g}'])
    lines.extend(f'M Q{q}' for q in range(6))
    return '\n'.join(lines)+'\n'


def evaluate_candidate(instance, candidate, penalized, gamma=.6, beta=.25, mode='optimize'):
    """Record each real objective call; the final distribution is calculated once."""
    validate_compute_parameters(instance['lambda'], instance['cost'], gamma, beta, mode)
    initial = [gamma, beta]*candidate['depth']
    evaluations = []
    def evaluate(parameters):
        _, probabilities = sdk_probabilities(qaoa_qcis(instance, candidate, parameters))
        value = float(probabilities@penalized)
        evaluations.append({'evaluation': len(evaluations)+1, 'parameters': parameters.tolist(), 'objective': value})
        return value
    if mode == 'optimize':
        result = minimize(evaluate, initial, method='COBYLA', options={'maxiter': 18, 'rhobeg': .35})
        parameters = result.x
        termination = {'nfev': int(result.nfev), 'success': bool(result.success),
                       'status': int(result.status), 'message': str(result.message),
                       'termination': 'OPTIMIZER_RETURNED', 'objective': float(result.fun)}
    else:
        parameters = np.asarray(initial, float)
        termination = {'nfev': 0, 'success': True, 'status': None,
                       'message': 'Fixed input parameters evaluated', 'termination': 'FIXED_PARAMETERS'}
    qcis = qaoa_qcis(instance, candidate, parameters)
    _, ideal = sdk_probabilities(qcis)
    if mode == 'fixed':
        termination['objective'] = float(ideal@penalized)
    trace = {'candidate': candidate['candidate_id'], 'mode': mode, 'initial_parameters': initial,
             'maxiter': 18 if mode == 'optimize' else 0, 'rhobeg': .35 if mode == 'optimize' else None,
             'evaluations': evaluations, 'parameters': parameters.tolist(), **termination}
    return qcis, ideal, trace


def portfolio_solution(instance, chosen, basket):
    """Expose the actual solver choice and exhaustive reference decomposition."""
    mu, sigma, previous = map(np.asarray, [instance['mu'], instance['sigma'], instance['previous']])
    k, risk_lambda, cost = instance['k'], instance['lambda'], instance['cost']
    exact = np.zeros(6); exact[list(basket)] = 1
    def components(x):
        weights = np.asarray(x)/k
        expected_return = float(mu@weights)
        variance = float(weights@sigma@weights)
        turnover = float(np.abs(weights-previous).sum())
        return {'expected_return': expected_return, 'negative_return': -expected_return,
                'variance': variance, 'risk_penalty': risk_lambda*variance, 'turnover': turnover,
                'transaction_cost': cost*turnover,
                'objective': objective(x, mu, sigma, k, risk_lambda, cost, previous)}
    chosen_basket = list(map(int, np.flatnonzero(chosen)))
    return {'schemaVersion': 'qf.portfolio-solution.v1', 'bit_order': 'array q0...q5; string q5...q0',
            'configuration': {'risk_lambda': risk_lambda, 'cost': cost, 'k': k},
            'chosen_bitvector': np.asarray(chosen, int).tolist(), 'chosen_basket': chosen_basket,
            'chosen_assets': [instance['asset_order'][i] for i in chosen_basket],
            'exhaustive_bitvector': exact.astype(int).tolist(), 'exhaustive_basket': list(basket),
            'exhaustive_assets': [instance['asset_order'][i] for i in basket],
            'chosen_components': components(chosen), 'exhaustive_components': components(exact)}
