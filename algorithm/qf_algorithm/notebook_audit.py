"""Read calculation artifacts and expose reproducible numerical checks."""
from __future__ import annotations

from dataclasses import dataclass, field
import ast
import hashlib
import inspect
import importlib
import json
from pathlib import Path
import textwrap
from typing import Any

import numpy as np
import pandas as pd

from .api import digest, load_json
from .legacy.bridge import objective, qubo, ising
from .legacy.quantum_search import circuit_resources, count_metrics
from .selection import LearningProxy


@dataclass
class RunView:
    """A read-only view of one completed calculation and its saved arrays."""
    directory: Path
    result: dict
    documents: dict[str, Any] = field(default_factory=dict)
    arrays: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def summary(self):
        return self.result.get('summary', self.result)

    @property
    def run_id(self):
        return self.result.get('runId', self.result.get('run_id', self.directory.name))

    @property
    def configuration(self):
        return self.result.get('configuration', self.result.get('config', {}))


def load_run(directory):
    """Load ordinary JSON/CSV/NPZ artifacts, with pickle loading disabled."""
    directory = Path(directory).resolve()
    result = load_json(directory / 'result.json')
    view = RunView(directory, result)
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix == '.json':
            view.documents[path.stem] = load_json(path)
        elif path.suffix == '.csv':
            view.tables[path.stem] = pd.read_csv(path, dtype={'bitstring': str, 'state': str, 'bitstring_q5_q0': str})
        elif path.suffix == '.npz':
            with np.load(path, allow_pickle=False) as values:
                view.arrays[path.stem] = {k: values[k].copy() for k in values.files}
    return view


def source_record(function):
    """Return the exact installed implementation, line location and file identity."""
    lines, first = inspect.getsourcelines(function)
    path = Path(inspect.getsourcefile(function)).resolve()
    text = ''.join(lines)
    return {'function': function.__module__ + '.' + function.__qualname__,
            'file': str(path), 'first_line': first, 'last_line': first + len(lines) - 1,
            'file_sha256': digest(path),
            'source_sha256': hashlib.sha256(text.encode()).hexdigest(), 'source': text}


def source_functions(topic):
    """Expand complete project definitions reachable from each page's calculation."""
    from . import api, data, risk, pipeline, quantum, analysis, notebook_measurements
    from .legacy import bridge, graph_head, quantum_features, quantum_search, stock_task, research_common, stock_report
    from .selection import build_request, validate_decision
    from .legacy.quantum_worker import qas30_protocol
    features = [stock_task.graph_and_risk, quantum_features.catalog, quantum_features.circuit_for,
                quantum_features.states, quantum_features.observables, graph_head.GraphHead,
                risk.fit_specific_risk, risk.predict_specific_risk]
    portfolio = [bridge.objective, bridge.qubo, bridge.ising, bridge.exhaustive, bridge.solve,
                 quantum.energies, quantum.portfolio_solution]
    circuits = [quantum_search.candidate_catalog, quantum.qaoa_qcis, quantum.evaluate_candidate,
                quantum_search.sdk_probabilities, quantum_search.noise_catalog,
                quantum_search.noisy_probabilities, circuit_resources, count_metrics]
    proxy = [LearningProxy, qas30_protocol._ridge_fit, qas30_protocol._ridge_predict,
             qas30_protocol._select_ridge_alpha, build_request, validate_decision]
    selections = {
        '00': [data.example_panel, pipeline.run_pipeline, *features, *portfolio, *circuits, *proxy],
        '01': [data.load_panel, data.from_returns, data.frozen_panel, data.validate_panel],
        '02': [pipeline.run_pipeline, *features],
        '03': [api._instance, api._demo_bridge, *portfolio],
        '04': [api._instance, api._demo_quantum, *portfolio, *circuits],
        '05': [pipeline.run_pipeline, *circuits, *proxy],
        '06': [analysis.recompute_primary_statistics, research_common.cvar95,
               research_common.paired_inference, research_common.holm,
               quantum_search.paired_problem_bootstrap, stock_report.matrix_for,
               stock_report.financial_contrasts, notebook_measurements.load_paired_measurements],
        '07': [data.frozen_panel, pipeline.run_pipeline, *features, *portfolio, *circuits, *proxy],
    }
    pending, found, seen, module_names = list(selections[str(topic)[:2]]), [], set(), {}
    while pending:
        item = pending.pop(0)
        if inspect.ismethod(item) and inspect.isclass(item.__self__):
            item = item.__self__
        if not (inspect.isfunction(item) or inspect.isclass(item)):
            continue
        if not item.__module__.startswith('qf_algorithm.') or item in seen:
            continue
        seen.add(item)
        found.append(source_record(item))
        owner = inspect.getmodule(item)
        namespace = dict(vars(owner))
        tree = ast.parse(textwrap.dedent(inspect.getsource(item)))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module_name = importlib.util.resolve_name('.'*node.level+(node.module or ''), owner.__package__) if node.level else node.module
                if module_name and module_name.startswith('qf_algorithm'):
                    imported = importlib.import_module(module_name)
                    for alias in node.names:
                        if alias.name != '*':
                            namespace[alias.asname or alias.name] = getattr(imported, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith('qf_algorithm'):
                        namespace[alias.asname or alias.name.split('.')[0]] = importlib.import_module(alias.name)
        module_names.setdefault(item.__module__, set()).update(
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load))
        def resolve(node):
            if isinstance(node, ast.Name):
                return namespace.get(node.id)
            if isinstance(node, ast.Attribute):
                parent = resolve(node.value)
                if inspect.ismodule(parent) or inspect.isclass(parent):
                    return getattr(parent, node.attr, None)
            return None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                dependency = resolve(node.func)
                if inspect.isfunction(dependency) or inspect.isclass(dependency) or inspect.ismethod(dependency):
                    pending.append(dependency)
    # Include imports and the actual constants referenced by these definitions,
    # such as bit order, noise matrices and the ridge regularization grid.
    for module_name, referenced in module_names.items():
        module = inspect.getmodule(next(item for item in seen if item.__module__ == module_name))
        path = Path(inspect.getsourcefile(module)).resolve()
        lines = path.read_text().splitlines(keepends=True)
        tree = ast.parse(''.join(lines))
        selected = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        assignments = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
        while True:
            added = []
            for node in assignments:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}
                if names & referenced and node not in selected:
                    added.append(node)
                    referenced.update(n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load))
            selected.extend(added)
            if not added:
                break
        selected.sort(key=lambda node: node.lineno)
        source = '\n'.join(''.join(lines[node.lineno-1:node.end_lineno]) for node in selected)
        found.append({'function': module_name+'：导入与计算常量', 'kind': 'MODULE_CONTEXT',
                      'file': str(path), 'first_line': min(node.lineno for node in selected),
                      'last_line': max(node.end_lineno for node in selected),
                      'line_ranges': [[node.lineno, node.end_lineno] for node in selected],
                      'file_sha256': digest(path), 'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                      'source': source})
    return found


def _check(rows, name, actual, expected=0., tolerance=1e-10, *, note=''):
    if isinstance(actual, (bool, np.bool_)):
        error = 0. if bool(actual) == bool(expected) else 1.
    elif np.asarray(actual).dtype.kind in 'biufc' and np.asarray(expected).dtype.kind in 'biufc':
        error = float(np.max(np.abs(np.asarray(actual) - np.asarray(expected))))
    else:
        error = 0. if np.array_equal(actual, expected) else 1.
    rows.append({'检查对象': name, '实测值': _scalar(actual), '预期值': _scalar(expected),
                 '最大误差': error, '容差': tolerance,
                 '结果': '通过' if np.isfinite(error) and error <= tolerance else '需核对', '依据': note})


def _scalar(value):
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    array = np.asarray(value)
    if array.dtype.kind not in 'biufc':
        return str(value)
    return float(array) if array.ndim == 0 else f'数组{array.shape}'


def portfolio_tables(instance, encoded=None):
    """Recompute all 64 states directly from the mathematical portfolio objective."""
    from .quantum import portfolio_energies
    bits = ((np.arange(64)[:, None] >> np.arange(6)) & 1).astype(float)
    mu, sigma, previous = [np.asarray(instance[k], dtype=float) for k in ('mu', 'sigma', 'previous')]
    risk_lambda, cost, k = instance['lambda'], instance['cost'], instance['k']
    _, values, penalized, optimum, penalty = portfolio_energies(instance)
    matrix, constant = qubo(mu, sigma, k, risk_lambda, cost, previous, penalty=penalty)
    h, j, shift = ising(matrix, constant)
    z = 1 - 2 * bits
    weights = bits / k
    direct = np.array([objective(x, mu, sigma, k, risk_lambda, cost, previous) for x in bits])
    expected = direct + penalty * (bits.sum(1) - k) ** 2
    frame = pd.DataFrame({
        '位串q5…q0': [f'{i:06b}' for i in range(64)],
        '持仓数量': bits.sum(1).astype(int), '可行组合': bits.sum(1) == k,
        '收益贡献': -weights @ mu,
        '风险贡献': risk_lambda * np.einsum('bi,ij,bj->b', weights, sigma, weights),
        '费用贡献': cost * np.abs(weights-previous).sum(1),
        '约束惩罚': penalty * (bits.sum(1)-k)**2,
        '组合原目标': direct, '含惩罚目标': expected,
        'QUBO目标': np.einsum('bi,ij,bj->b', bits, matrix, bits)+constant,
        'Ising目标': z@h+np.einsum('bi,ij,bj->b', z, j, z)+shift})
    return frame, matrix, {'penalty': penalty, 'optimum': optimum, 'h': h, 'J': j, 'offset': shift}


def audit_run(view):
    """Check saved identities and independently reconstruct the displayed numbers."""
    rows = []
    result, documents, arrays = view.result, view.documents, view.arrays
    checksum = view.directory / 'result.sha256'
    if checksum.exists():
        expected = checksum.read_text().split()[0]
        _check(rows, '结果文件SHA', digest(view.directory/'result.json') == expected, True, 0)
    manifest = result.get('artifacts', [])
    if isinstance(manifest, dict):
        manifest = [dict(path=k, **v) if isinstance(v, dict) else {'path': k, 'sha256': v}
                    for k, v in manifest.items()]
    for item in manifest:
        path = view.directory / item['path']
        if 'sha256' in item:
            _check(rows, '工件SHA：'+item['path'], path.is_file() and digest(path) == item['sha256'], True, 0)
    panel = documents.get('input', documents.get('normalized_input'))
    if panel:
        from .data import validate_panel
        validate_panel(panel)
        _check(rows, '资产轴唯一数量', len(set(panel['asset_order'])), 6, 0)
        _check(rows, '日期严格有序', panel['dates'] == sorted(set(panel['dates'])), True, 0)
        _check(rows, '观测标签与目标区间', True, True, 0,
               note='validate_panel逐项验证下一开盘至随后开盘的目标')
    risk = documents.get('risk_artifact')
    if risk:
        sigma = np.asarray(risk['Sigma'])
        _check(rows, '协方差对称性', np.max(abs(sigma-sigma.T)), 0)
        minimum = float(np.linalg.eigvalsh(sigma).min())
        _check(rows, '协方差最小特征值界', max(0., -minimum), 0,
               note=f'最小特征值={minimum:.12g}')
        factor = risk['factor_decomposition']
        loading, covariance = np.asarray(factor['Lambda']), np.asarray(factor['F'])
        if risk['risk_mode'] in {'factor', 'quantum_specific'}:
            specific = factor['d'] if risk['risk_mode'] == 'factor' else risk['predicted_specific_variance']
            reconstruction = loading@covariance@loading.T+np.diag(specific)
            _check(rows, '因子项与特定方差重建', reconstruction, sigma)
        _check(rows, '训练标签成熟时点', risk['latest_training_label_available'] < risk['evaluation_dates'][0], True, 0)
        if 'risk_arrays' in arrays:
            readout = arrays['risk_arrays']['readout']
            _check(rows, '量子读出维数', readout.shape[-1], 11, 0)
            _check(rows, '量子期望值范围', max(0., float(abs(readout).max())-1.), 0)
            saved = arrays.get('mean_model')
            if saved:
                from .legacy.graph_head import GraphHead
                head = GraphHead(saved['W1'].shape[0], saved['W1'].shape[1], 2 if 'W2' in saved else 1, 0., 0)
                for key in head.parameters:
                    head.parameters[key] = saved[key].copy()
                for key in ('xmean', 'xscale'):
                    setattr(head, key, saved[key].copy())
                head.ymean, head.yscale = float(saved['ymean']), float(saved['yscale'])
                prediction = head.predict(readout, arrays['risk_arrays']['graphs'])
                _check(rows, '保存模型的末日预测', prediction[-1], risk['mu'])
    instance = documents.get('bridge_instance')
    if instance:
        states, _, _ = portfolio_tables(instance, documents.get('qubo'))
        _check(rows, '可行三资产组合数', int(states['可行组合'].sum()), 20, 0)
        _check(rows, '原目标与QUBO', states['含惩罚目标'].values, states['QUBO目标'].values)
        _check(rows, '原目标与Ising', states['含惩罚目标'].values, states['Ising目标'].values)
    quantum = documents.get('quantum_results', [])
    if isinstance(quantum, list) and quantum:
        from .quantum import portfolio_energies
        _, values, penalized, optimum, _ = portfolio_energies(instance)
        for record in quantum:
            candidate = record['candidate']
            counts = record['counts']
            _check(rows, candidate+'的shots总数', sum(counts.values()), result['configuration']['shots'], 0)
            metric = count_metrics(counts, values, penalized, optimum)
            for name, value in metric.items():
                if name in record and isinstance(value, (float, int)):
                    _check(rows, candidate+'：'+name, record[name], value)
            resource = circuit_resources((view.directory/(candidate+'.qcis')).read_text())
            for name, value in resource.items():
                if name in record:
                    _check(rows, candidate+'：'+name, record[name], value, 0)
    if 'quantum_probabilities' in arrays:
        probabilities = arrays['quantum_probabilities']
        for key in ('ideal', 'noisy'):
            if key in probabilities:
                _check(rows, key+'概率归一', probabilities[key].sum(-1), np.ones(probabilities[key].shape[:-1]))
                _check(rows, key+'概率非负', max(0., -float(probabilities[key].min())), 0)
    if 'distribution' in view.tables:
        from .quantum import portfolio_energies
        _, values, penalized, optimum, _ = portfolio_energies(instance)
        distribution = view.tables['distribution']
        metrics = view.tables['metrics']
        for (candidate, noise), part in distribution.groupby(['candidate', 'noise']):
            counts = dict(zip(part.bitstring_q5_q0, map(int, part['count'])))
            _check(rows, candidate+'的64状态位序', part.bitstring_q5_q0.tolist(), [f'{i:06b}' for i in range(64)], 0)
            _check(rows, candidate+'的shots总数', sum(counts.values()), view.summary['shots_per_setting'], 0)
            calculated = count_metrics(counts, values, penalized, optimum)
            saved = metrics.loc[(metrics.candidate==candidate)&(metrics.noise==noise)].iloc[0]
            for key, value in calculated.items():
                if key in saved and isinstance(value, (int, float)):
                    _check(rows, candidate+'：'+key, saved[key], value)
            resource = circuit_resources((view.directory/(candidate+'.qcis')).read_text())
            for key,value in resource.items():
                if key in saved:
                    _check(rows, candidate+'：'+key, saved[key], value, 0)
    trace = documents.get('optimization_trace')
    if trace:
        for row in trace['candidates']:
            if row['mode'] == 'optimize':
                _check(rows, row['candidate']+'的实际求值记录', len(row['evaluations']), row['nfev'], 0)
    if 'candidate_audit' in view.tables:
        table = view.tables['candidate_audit']
        from .selection import FEATURES
        proxy = LearningProxy.load(view.directory/'learning_proxy.json')
        predicted = proxy.predict(table[list(FEATURES)].to_dict('records'))
        _check(rows, '代理预测分数复算', predicted, table['prediction'].to_numpy())
        _check(rows, '代理入选数量', int(table['selected'].sum()), 5, 0)
    return pd.DataFrame(rows)


def artifact_inventory(view):
    """List each artifact used by this page with its complete byte identity."""
    return pd.DataFrame([{'文件': p.name, '字节数': p.stat().st_size, 'SHA256': digest(p),
                          '位置': str(p)} for p in sorted(view.directory.iterdir())
                         if p.is_file() and not p.is_symlink()])


def compare_runs(views):
    """Compare two or three completed runs with their effective configuration."""
    views = list(views)
    if not 2 <= len(views) <= 3:
        raise ValueError('COMPARE_REQUIRES_TWO_OR_THREE_RUNS')
    rows = []
    for view in views:
        row = {'运行': view.run_id, **{'配置.'+key: value for key, value in view.configuration.items()}}
        row['实际耗时秒'] = view.result.get('notebook', {}).get('wall_seconds')
        row.update({'结果.'+key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (dict, list, tuple)) else value
                    for key, value in view.summary.items()})
        row['结果SHA256'] = digest(view.directory/'result.json')
        row['结果文件'] = str(view.directory/'result.json')
        rows.append(row)
    return pd.DataFrame(rows)
