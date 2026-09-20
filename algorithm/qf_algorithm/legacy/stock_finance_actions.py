"""Event-bound share exchange on frozen stock predictions and financial contracts.

The original financial implementation and artifacts remain independent parents.
This execution version inherits development selections and reruns the complete
original test enumeration with a causal, named-security pending-exit policy.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import resource
import time

import numpy as np
from . import stock_finance as base
from .research_common import digest, identity, write_json, utc, cvar95

EVENT_SHA256 = 'e2c0199dda1d20b1435e2a27de81e0c25dd3a33fb68524bb4be7205d2cb46672'
SCHEMA = 'qf.stock-finance-corporate-actions-freeze.v2'
PENDING_POLICY = 'NAMED_SECURITY_FULL_EXIT_WITH_SIGNAL_VISIBLE_TARGET_EXCLUSION'


def load(path):
    return json.loads(Path(path).read_text())


def event_contract(events, data_manifest_sha, expected_sha=None, path=None):
    if expected_sha is not None and (path is None or digest(path) != expected_sha):
        raise ValueError('CORPORATE_EVENT_MANIFEST_SHA')
    if events.get('schema_version') != 'qf.corporate-action-events.v1' or events['parent_data_manifest_sha256'] != data_manifest_sha:
        raise ValueError('CORPORATE_EVENT_DATA_PARENT')
    if not isinstance(events.get('events'), list) or not events['events']:
        raise ValueError('NONEMPTY_EVENT_SET')
    ids = set()
    for event in events['events']:
        if event['event_id'] in ids or event['source_asset'] == event['target_asset']:
            raise ValueError('UNIQUE_CORPORATE_EVENT')
        ids.add(event['event_id'])
        if event['event_type'] != 'SHARE_EXCHANGE' or event['conversion_availability'] != 'NEW_A_SHARES_LISTING_OPEN':
            raise ValueError('SHARE_EXCHANGE_AVAILABILITY')
        if event['holdings_basis'] != 'ORDINARY_UNRESTRICTED_A_SHARES' or event['fractional_share_policy'] != 'CONTINUOUS_FRACTIONAL_RESEARCH_POSITION':
            raise ValueError('SHARE_BASIS')
        if (type(event['ratio_numerator']) is not int or type(event['ratio_denominator']) is not int or
            event['ratio_numerator'] <= 0 or event['ratio_denominator'] <= 0):
            raise ValueError('POSITIVE_EXACT_EXCHANGE_RATIO')
        known = event['known_by']
        if not known.endswith('_CLOSE') or len(known) != 14:
            raise ValueError('EVENT_CLOSE_INFORMATION_TIME')
        if not (event['record_date'] <= event['delisting_date'] <= event['registration_completed_date'] <=
                event['announcement_date'] == known[:8] < event['effective_open_date']):
            raise ValueError('EVENT_CALENDAR_ORDER')
    return events


class CorporateActions:
    def __init__(self, events, market):
        self.events = events['events']
        self.market = market
        self.date_index = {str(v): i for i, v in enumerate(market['dates'])}
        self.asset_index = {str(v): i for i, v in enumerate(market['assets'])}
        self.processed = set()
        for event in self.events:
            if event['effective_open_date'] not in self.date_index:
                raise ValueError('CORPORATE_EVENT_OPEN_IN_CALENDAR')
            if event['source_asset'] not in self.asset_index or event['target_asset'] not in self.asset_index:
                raise ValueError('CORPORATE_EVENT_ASSET_AXIS')

    def pending_for_signal(self, index, positions, pending):
        """Only known event fields and currently owned positions define exclusions."""
        filtered = dict(pending)
        exclusions = []
        date = str(self.market['dates'][index])
        for event in self.events:
            if date < event['known_by'][:8]:
                continue
            source = self.asset_index[event['source_asset']]
            target = self.asset_index[event['target_asset']]
            # The delisted source is unavailable for fresh entry after this
            # event's information time, independently of a future opening bar.
            filtered.setdefault(source, index)
            exclusions.append({'event_id': event['event_id'], 'asset_index': source, 'reason': 'KNOWN_DELISTED_SOURCE'})
            if (date < event['effective_open_date'] and source in pending and positions.get(source, 0.) > 0):
                filtered[target] = min(filtered.get(target, pending[source]), pending[source])
                exclusions.append({'event_id': event['event_id'], 'asset_index': target,
                                   'reason': 'KNOWN_PENDING_SHARE_EXCHANGE_TARGET'})
        return filtered, exclusions

    def apply_open(self, index, positions, cash, pending):
        positions, pending = dict(positions), dict(pending)
        rows = []
        date = str(self.market['dates'][index])
        for event in self.events:
            if event['event_id'] in self.processed or date != event['effective_open_date']:
                continue
            self.processed.add(event['event_id'])
            source = self.asset_index[event['source_asset']]
            target = self.asset_index[event['target_asset']]
            units = positions.get(source, 0.)
            if units <= 0:
                rows.append({'event_id': event['event_id'], 'date': date, 'status': 'SOURCE_POSITION_ABSENT',
                             'conversion_cost': 0., 'cash_before': cash, 'cash_after': cash})
                continue
            before, _ = base.nav_at(self.market, index, positions, cash, 'open')
            factor = self.market['adj_factor'][:, source]
            eligible = np.flatnonzero((self.market['dates'].astype(str) <= event['record_date']) & np.isfinite(factor) & (factor > 0))
            if not len(eligible):
                raise ValueError('RECORD_DATE_SOURCE_ADJUSTMENT_FACTOR')
            record_index = int(eligible[-1])
            old_factor = float(factor[record_index])
            new_factor = float(self.market['adj_factor'][index, target])
            if not np.isfinite(new_factor) or new_factor <= 0:
                raise ValueError('AVAILABLE_OPEN_TARGET_ADJUSTMENT_FACTOR')
            old_shares = float(units * old_factor)
            ratio = event['ratio_numerator'] / event['ratio_denominator']
            new_shares = old_shares * ratio
            added_units = new_shares / new_factor
            existing = float(positions.get(target, 0.))
            source_pending = pending.pop(source, None)
            target_pending = pending.get(target)
            positions.pop(source)
            positions[target] = existing + added_units
            if source_pending is not None:
                pending[target] = min(source_pending, target_pending) if target_pending is not None else source_pending
            after, _ = base.nav_at(self.market, index, positions, cash, 'open')
            if (not math.isclose(added_units * new_factor, old_shares * ratio, rel_tol=1e-12, abs_tol=1e-14) or
                not math.isclose(positions[target] - existing, added_units, rel_tol=1e-12, abs_tol=1e-14)):
                raise ValueError('CORPORATE_SHARE_QUANTITY_CONSERVATION')
            rows.append({'event_id': event['event_id'], 'date': date, 'status': 'CONVERTED',
                'source_asset': event['source_asset'], 'target_asset': event['target_asset'],
                'source_asset_index': source, 'target_asset_index': target,
                'source_adjusted_units': units, 'source_record_factor': old_factor,
                'source_factor_date': str(self.market['dates'][record_index]),
                'source_original_share_equivalent': old_shares,
                'ratio_numerator': event['ratio_numerator'], 'ratio_denominator': event['ratio_denominator'],
                'target_original_share_equivalent': new_shares, 'target_available_open_factor': new_factor,
                'added_target_adjusted_units': added_units, 'existing_target_adjusted_units': existing,
                'merged_target_adjusted_units': positions[target],
                'source_mark_price': float(self.market['mark_open'][index, source]),
                'target_mark_price': float(self.market['mark_open'][index, target]),
                'source_mark_index': int(self.market['open_mark_index'][index, source]),
                'target_mark_index': int(self.market['open_mark_index'][index, target]),
                'cash_before': cash, 'cash_after': cash, 'conversion_cost': 0.,
                'nav_before_conversion': before, 'nav_after_conversion': after,
                'conversion_revaluation_value': after - before,
                'source_pending_since': source_pending, 'target_pending_before': target_pending,
                'merged_pending_since': pending.get(target), 'pending_policy': PENDING_POLICY})
        return positions, cash, pending, rows


def execute_event_open(actions, market, index, positions, cash, desired, pending, cost, k):
    positions, cash, pending, events = actions.apply_open(index, positions, cash, pending)
    positions, cash, pending, row = base.execute_open(market, index, positions, cash, desired, pending, cost, k)
    row['corporate_actions'] = events
    row['corporate_action_revaluation_value'] = sum(e.get('conversion_revaluation_value', 0.) for e in events)
    return positions, cash, pending, row


def simulate_path(market, signal_indices, planner, cost, events, k=5, tail=True):
    if not signal_indices or np.any(np.diff(signal_indices) != 1):
        raise ValueError('SIGNAL_CALENDAR_CONTINUITY')
    actions = CorporateActions(events, market)
    positions, cash, pending, opening, signals = {}, 1., {}, [], []
    for index in signal_indices:
        nav, values = base.nav_at(market, index, positions, cash, 'close')
        visible_pending, exclusions = actions.pending_for_signal(index, positions, pending)
        plan = planner(index, positions, visible_pending, nav, values)
        plan['corporate_event_signal_exclusions'] = exclusions
        signals.append(plan)
        positions, cash, pending, row = execute_event_open(actions, market, index + 1, positions, cash,
                                                         plan['priority_global'], pending, cost, k)
        row['signal_index'] = index
        opening.append(row)
    terminal = signal_indices[-1] + 2
    positions, cash, pending, row = execute_event_open(actions, market, terminal, positions, cash, [], pending, cost, k)
    row.update(signal_index=None, purpose='COMMON_TERMINAL_EXIT')
    opening.append(row)
    intervals = []
    for offset, index in enumerate(signal_indices):
        entry, end = opening[offset], opening[offset + 1]
        denominator = 1. if offset == 0 else entry['nav_after']
        initial_cost = entry['cost'] if offset == 0 else 0.
        fees = initial_cost + end['cost']
        turnover = (entry['turnover'] if offset == 0 else 0.) + end['turnover']
        net = end['nav_after'] / denominator - 1
        gross = (end['nav_before'] - entry['nav_after']) / denominator
        if abs(net - (gross - fees / denominator)) > 1e-10:
            raise ValueError('INTERVAL_PROFIT_AND_COST')
        intervals.append({'signal_index': index, 'signal_date': str(market['dates'][index]),
            'entry_date': entry['date'], 'exit_date': end['date'], 'net_return': net, 'net_loss': -net,
            'gross_return': gross, 'cost_fraction': fees / denominator, 'cost_value': fees,
            'turnover_fraction': turnover / denominator, 'turnover_value': turnover,
            'corporate_action_revaluation_value': end['corporate_action_revaluation_value'],
            'nav_denominator': denominator, 'nav_end': end['nav_after'],
            'open_to_open_after_entry_return': end['nav_after'] / entry['nav_after'] - 1})
    returns = np.array([r['net_return'] for r in intervals])
    wealth = np.r_[1., [r['nav_after'] for r in opening]]
    if abs(np.prod(1 + returns) - wealth[-1]) > 1e-9:
        raise ValueError('COMPOUNDING_CONSERVATION')
    if abs(sum(r['cost_value'] for r in intervals) - sum(r['cost'] for r in opening)) > 1e-10:
        raise ValueError('SINGLE_COST_ALLOCATION')
    terminal_positions = copy.deepcopy(opening[-1]['holdings'])
    tail_rows = []
    if tail:
        for index in range(terminal + 1, len(market['dates'])):
            if not positions:
                break
            positions, cash, pending, row = execute_event_open(actions, market, index, positions, cash, [], pending, cost, k)
            row['purpose'] = 'POST_HORIZON_LIQUIDATION'
            tail_rows.append(row)
    return {'signal_records': signals, 'opening_records': opening, 'interval_records': intervals,
        'nav_path': wealth.tolist(), 'terminal_positions_at_primary_horizon': terminal_positions,
        'liquidation_tail': tail_rows,
        'remaining_positions_after_observed_tail': [{'asset_index': a, 'units': v} for a, v in positions.items()],
        'summary': {'cvar95_net_loss': float(cvar95(-returns)), 'mean_net_return': float(returns.mean()),
            'maximum_drawdown': float(np.max(1 - wealth / np.maximum.accumulate(wealth))),
            'terminal_nav': float(wealth[-1]), 'primary_cost_value': sum(r['cost'] for r in opening),
            'primary_turnover_value': sum(r['turnover'] for r in opening),
            'delayed_exit_count': sum(len(r['delayed_exits']) for r in opening),
            'primary_corporate_action_revaluation_value': sum(r['corporate_action_revaluation_value'] for r in opening),
            'conversion_count': sum(e['status'] == 'CONVERTED' for r in opening for e in r['corporate_actions']),
            'liquidation_tail_cost_value': sum(r['cost'] for r in tail_rows)}}


def supervisor_inventory(directory, legacy_root):
    found = {}
    for path in sorted(Path(directory).glob('*/state.json')):
        row = load(path)
        command = row.get('command', [])
        f = {v[2:]: command[i + 1] for i, v in enumerate(command[:-1]) if isinstance(v, str) and v.startswith('--')}
        if f.get('finance-root') != str(Path(legacy_root).resolve()):
            continue
        script = next((Path(v).name for v in command if isinstance(v, str) and v.endswith('.py')), '')
        si = next((i for i, v in enumerate(command) if isinstance(v, str) and v.endswith('.py')), -1)
        mode = command[si + 1] if si >= 0 and si + 1 < len(command) and not command[si + 1].startswith('--') else ''
        key = (script, mode, f.get('job-id') or f.get('seed') or f.get('group') or '')
        if key in found:
            raise ValueError('UNIQUE_LEGACY_SUPERVISOR_IDENTITY')
        found[key] = (row, {'path': str(path.resolve()), 'sha256': digest(path)})
    return found


def legacy_budget(legacy_root, doc, supervisors):
    """Charge terminal work; reserve every uncompleted original frozen unit."""
    root = Path(legacy_root)
    inventory = supervisor_inventory(supervisors, root)
    groups = {g: [] for g in ['E04', 'E07']}

    def charge(group, key, cap, ledger_path=None, label=None):
        state, reference = inventory.get(key, (None, None))
        finished = state is not None and state.get('state') not in {'RUNNING', 'PREPARED'} and state.get('finished_at') is not None
        amount, rule = cap, 'ORIGINAL_UNIT_CAP_RESERVED'
        lref = None
        if finished:
            amount = float(state['cpu_seconds'])
            if not math.isfinite(amount) or amount < 0:
                raise ValueError('LEGACY_PROCESS_CPU')
            rule = 'MAX_TERMINAL_SUPERVISOR_AND_INTERNAL_CPU'
            if ledger_path is not None and Path(ledger_path).exists():
                ledger = load(ledger_path)
                amount = max(amount, float(ledger['used']['cpu_seconds']))
                lref = {'path': str(Path(ledger_path).resolve()), 'sha256': digest(ledger_path)}
        groups[group].append({'unit': label or list(key), 'cpu_seconds_charged_or_reserved': amount,
                              'rule': rule, 'original_cpu_cap': cap, 'supervisor_source': reference,
                              'internal_source': lref})

    for j in doc['jobs']:
        charge(j['group'], ('stock_finance.py', 'run-path', j['job_id']), j['cpu_seconds_cap'],
               root / 'paths' / j['job_id'] / 'resource_ledger.json')
    for seed in doc['seeds']:
        charge('E04', ('stock_finance.py', 'risk-head', str(seed)), 2400,
               root / 'risk_heads' / ('seed' + str(seed)) / 'resource_ledger.json')
    charge('E04', ('stock_finance.py', 'partition', ''), 2000, root / 'prediction_partitions/resource_ledger.json')
    for group in groups:
        for mode, cap in [('select-finance', 2000), ('report-finance', 8000 if group == 'E04' else 10000)]:
            charge(group, ('stock_report.py', mode, group), cap,
                   root / (mode.split('-')[0] + '_' + group + '.budget') / 'resource_ledger.json')
        key = ('power_diagnostics.py', '', group)
        if key in inventory:
            charge(group, key, 1000, root / 'power_diagnostics' / group / 'resource_ledger.json')
    return groups


def freeze(stock_root, data_root, legacy_root, event_path, supervisors, output, expected_event_sha=EVENT_SHA256):
    started = time.process_time()
    stock_root, data_root, legacy_root, event_path, output = map(Path, [stock_root, data_root, legacy_root, event_path, output])
    if output.exists():
        raise FileExistsError('NEW_CORPORATE_ACTION_RUN_ROOT_REQUIRED')
    original = load(legacy_root / 'freeze.json')
    base.validate_finance_freeze(original, stock_root / 'freeze.json', data_root / 'manifest.json')
    events = event_contract(load(event_path), original['data_manifest_sha256'], expected_event_sha, event_path)
    dates = load(data_root / 'manifest.json')['dates']
    if any(str(dates[i]) >= min(e['known_by'][:8] for e in events['events']) for i in original['development_indices']):
        raise ValueError('INHERITED_DEVELOPMENT_PRECEDES_EVENT_INFORMATION')
    original_sha = digest(legacy_root / 'freeze.json')
    parents = {}
    for group in ['E04', 'E07']:
        path = legacy_root / ('selection_' + group + '.json')
        selection = load(path)
        if selection['finance_freeze_sha256'] != original_sha:
            raise ValueError('INHERITED_SELECTION_FREEZE')
        parents[group] = {'path': str(path.resolve()), 'sha256': digest(path), 'lambda': selection['lambda'],
                          'block_length': selection['block']['length']}
    legacy_charges = legacy_budget(legacy_root, original, supervisors)
    jobs = []
    groups = copy.deepcopy(original['groups'])
    for group in groups:
        test = [j for j in original['jobs'] if j['group'] == group and j['phase'] == 'test']
        expected = len(base.METHODS[group]) * len(original['seeds']) * len(groups[group]['solver_contract']['cost_levels'])
        if len(test) != expected or sum(j['solver_jobs'] for j in test) != groups[group]['solver_contract']['final_jobs']:
            raise ValueError('COMPLETE_ORIGINAL_TEST_ENUMERATION')
        for old in test:
            new = copy.deepcopy(old)
            new.update(job_id='ca_v2__' + old['job_id'], original_job_id=old['job_id'], solver_cpu_cap=1,
                       overhead_cpu_seconds=30, cpu_seconds_cap=old['solver_jobs'] + 30)
            jobs.append(new)
        previous = sum(r['cpu_seconds_charged_or_reserved'] for r in legacy_charges[group])
        final_reserved = sum(j['cpu_seconds_cap'] for j in jobs if j['group'] == group)
        statistics_reserved = 8000 if group == 'E04' else 10000
        total = previous + final_reserved + statistics_reserved + 500
        if total > original['groups'][group]['total_cpu_seconds_cap']:
            raise ValueError('BUDGET_INFEASIBLE:' + group + ':' + str(total))
        groups[group]['corporate_actions_budget'] = {
            'legacy_charged_or_reserved_cpu_seconds': previous, 'legacy_sources': legacy_charges[group],
            'new_path_count': len(test), 'new_solver_jobs': sum(j['solver_jobs'] for j in test),
            'new_solver_cpu_cap_per_job': 1, 'new_overhead_cpu_seconds_per_path': 30,
            'new_path_cpu_seconds_reserved': final_reserved, 'new_statistics_cpu_seconds_reserved': statistics_reserved,
            'new_preparation_cpu_seconds_reserved': 500, 'cumulative_cpu_seconds_charged_or_reserved': total,
            'cumulative_cpu_seconds_cap': original['groups'][group]['total_cpu_seconds_cap']}
        groups[group]['solver_contract'].update(development_jobs=0, final_jobs=sum(j['solver_jobs'] for j in test),
            total_jobs=sum(j['solver_jobs'] for j in test), cpu_seconds_cap_per_job=1,
            reserved_cpu_seconds=sum(j['solver_jobs'] for j in test), development_reservation=0,
            final_reservation=sum(j['solver_jobs'] for j in test))
        groups[group].update(path_jobs=len(test), solver_phase_counts={'development': 0, 'test': sum(j['solver_jobs'] for j in test)},
            reserved_solver_cpu_seconds=sum(j['solver_jobs'] for j in test), overhead_cpu_seconds=30 * len(test),
            risk_head_cpu_seconds=0, preparation_and_statistics_cpu_seconds=statistics_reserved + 500)
    if len(jobs) != 115 or {g: sum(j['group'] == g for j in jobs) for g in groups} != {'E04': 100, 'E07': 15}:
        raise ValueError('FROZEN_115_TEST_PATHS')
    partitions = legacy_root / 'prediction_partitions/manifest.json'
    if load(partitions)['finance_freeze_sha256'] != original_sha:
        raise ValueError('REUSED_PREDICTION_PARENT')
    risks = []
    for seed in original['seeds']:
        path = legacy_root / 'risk_heads' / ('seed' + str(seed)) / 'manifest.json'
        if load(path)['finance_freeze_sha256'] != original_sha:
            raise ValueError('REUSED_RISK_HEAD_PARENT')
        risks.append({'seed': seed, 'path': str(path.resolve()), 'sha256': digest(path)})
    doc = copy.deepcopy(original)
    doc.update(schema_version=SCHEMA, created_at=utc(), data_epoch=events['data_epoch'],
        parent_data_epoch=original['data_epoch'], legacy_finance_root=str(legacy_root.resolve()),
        legacy_finance_freeze_sha256=original_sha, event_manifest_path=str(event_path.resolve()),
        event_manifest_sha256=expected_event_sha, jobs=jobs, groups=groups,
        inherited_selections=parents, reused_prediction_partition={'path': str(partitions.resolve()), 'sha256': digest(partitions)},
        reused_risk_heads=risks, financial_endpoints_evaluated=0,
        parent_result_availability='PARENT_V1_RESULTS_EXIST',
        revision_reason='OFFICIAL_SHARE_EXCHANGE_EXECUTION_EVENT',
        selection_reuse_rule='Same 2023 development bytes; event information and execution begin in 2025; lambda and block are inherited by SHA.',
        pending_exit_rule=PENDING_POLICY,
        corporate_action_rule={
            'quantity': 'old adjusted_units * last finite positive adjustment factor at or before record_date * exact ratio / target adjustment factor at available open',
            'existing_target': 'sum existing and newly converted adjusted units; inherit earliest pending marker; pending applies to the complete merged named security',
            'signal': 'after known_by close exclude delisted source; before availability exclude target when a currently held source already has a pending exit',
            'conversion': 'zero fee and unchanged cash; opening market revaluation enters holding-period return and is recorded separately',
            'trading': 'base net opening fills, price/volume/limit gates, transaction costs, K slots and shared primary horizon',
            'valuation_before_availability': 'parent last-observed adjusted close/open mark proxy',
            'data_scope': 'ordinary unrestricted A-share exchange with continuous fractional research positions'},
        implementation=[*original['implementation'], {'name': Path(__file__).name, 'sha256': digest(__file__)}])
    budget = base.phase_budget(output / 'preparation_budget', {'cpu_seconds': 500})
    budget.cpu_start = started
    write_json(output / 'freeze.json', doc)
    for group, parent in parents.items():
        selection = load(parent['path'])
        selection.update(finance_freeze_sha256=digest(output / 'freeze.json'),
                         parent_selection_sha256=parent['sha256'], parent_finance_freeze_sha256=original_sha,
                         reuse_basis='UNCHANGED_2023_DEVELOPMENT_BEFORE_2025_EVENT', event_manifest_sha256=expected_event_sha)
        write_json(output / ('selection_' + group + '.json'), selection)
    budget.save()
    if budget.used['cpu_seconds'] > 500:
        raise ValueError('CORPORATE_ACTION_FREEZE_CPU_CAP')
    write_json(output / 'preparation_receipt.json', {'freeze_sha256': digest(output / 'freeze.json'),
        'resource_ledger_sha256': digest(budget.path), 'created_at': utc(), 'new_model_training': 0,
        'new_quantum_calls': 0, 'shots': 0, 'new_financial_test_paths': len(jobs)})
    return {'path': str(output / 'freeze.json'), 'sha256': digest(output / 'freeze.json'), 'path_jobs': len(jobs),
            'budgets': {g: groups[g]['corporate_actions_budget']['cumulative_cpu_seconds_charged_or_reserved'] for g in groups}}


def run_path(stock_root, data_root, finance_root, job_id):
    started = time.process_time()
    stock_root, data_root, finance_root = map(Path, [stock_root, data_root, finance_root])
    doc = load(finance_root / 'freeze.json')
    if doc['schema_version'] != SCHEMA:
        raise ValueError('CORPORATE_ACTION_EXECUTION_IDENTITY')
    base.validate_finance_freeze(doc, stock_root / 'freeze.json', data_root / 'manifest.json')
    legacy_root = Path(doc['legacy_finance_root'])
    if digest(legacy_root / 'freeze.json') != doc['legacy_finance_freeze_sha256']:
        raise ValueError('LEGACY_FINANCE_PARENT_SHA')
    events = event_contract(load(doc['event_manifest_path']), doc['data_manifest_sha256'], doc['event_manifest_sha256'], doc['event_manifest_path'])
    matched = [j for j in doc['jobs'] if j['job_id'] == job_id]
    if len(matched) != 1 or matched[0]['phase'] != 'test':
        raise ValueError('CORPORATE_ACTION_TEST_JOB')
    job = matched[0]
    selection_path = finance_root / ('selection_' + job['group'] + '.json')
    selection = load(selection_path)
    parent = doc['inherited_selections'][job['group']]
    if (selection['finance_freeze_sha256'] != digest(finance_root / 'freeze.json') or
        selection['parent_selection_sha256'] != parent['sha256'] or digest(parent['path']) != parent['sha256'] or
        selection['lambda'] != parent['lambda'] or selection['block']['length'] != parent['block_length']):
        raise ValueError('UNCHANGED_DEVELOPMENT_SELECTION')
    lam, selection_sha = selection['lambda'], digest(selection_path)
    out = finance_root / 'paths' / job_id
    cap = {'cpu_seconds': job['cpu_seconds_cap'], 'solver_cpu_seconds': job['solver_jobs'] * job['solver_cpu_cap'], 'solver_jobs': job['solver_jobs']}
    budget = base.phase_budget(out, cap, 'execution')
    budget.cpu_start = started
    partition = doc['reused_prediction_partition']
    if digest(partition['path']) != partition['sha256']:
        raise ValueError('REUSED_PREDICTION_PARTITION_SHA')
    family, risk = base.METHODS[job['group']][job['method']]
    predictions, prediction_record = base.read_prediction(legacy_root, family, job['seed'], job['phase'], 'directional' if family in ['selected', 'fixed'] else None)
    classic, classic_record = base.read_prediction(legacy_root, 'classical', job['seed'], job['phase'])
    if predictions['date_indices'].tolist() != job['signal_indices'] or classic['date_indices'].tolist() != job['signal_indices']:
        raise ValueError('FINANCIAL_PREDICTION_DATES')
    data = base.load_finance_panels(data_root)
    market, days = base.prepare_market(data), base.graph_lookup(stock_root)
    predicted_d = risk_manifest_sha = None
    if risk == 'readout':
        binding = next(r for r in doc['reused_risk_heads'] if r['seed'] == job['seed'])
        if digest(binding['path']) != binding['sha256']:
            raise ValueError('REUSED_RISK_HEAD_SHA')
        rr, rm = Path(binding['path']).parent, load(binding['path'])
        if rm['finance_freeze_sha256'] != doc['legacy_finance_freeze_sha256']:
            raise ValueError('RISK_HEAD_PARENT_FREEZE')
        rp = next(r for r in rm['outputs'] if r['phase'] == job['phase'])
        if digest(rr / rp['path']) != rp['sha256']:
            raise ValueError('RISK_HEAD_PREDICTION_SHA')
        with np.load(rr / rp['path'], allow_pickle=False) as z:
            predicted_d = {k: z[k] for k in z.files}
        if predicted_d['date_indices'].tolist() != job['signal_indices']:
            raise ValueError('RISK_HEAD_PREDICTION_DATES')
        risk_manifest_sha = binding['sha256']
    position_map = {index: n for n, index in enumerate(job['signal_indices'])}
    write_json(out / 'intent.json', {'job': job, 'lambda': lam, 'finance_freeze_sha256': digest(finance_root / 'freeze.json'),
        'selection_sha256': selection_sha, 'event_manifest_sha256': doc['event_manifest_sha256'],
        'prediction_source': prediction_record, 'risk_head_manifest_sha256': risk_manifest_sha, 'created_at': utc()})

    def planner(index, positions, pending, nav, held_values):
        row, record = position_map[index], days[index]
        day = base.read_graph(stock_root, record)
        np.testing.assert_array_equal(day['assets'], predictions['assets'][row])
        np.testing.assert_array_equal(day['assets'], classic['assets'][row])
        axis, mu, factor_sigma = day['asset_indices'], predictions['mu'][row], day['Sigma']
        sigma = factor_sigma if risk == 'factor' else day['shrinkage_Sigma']
        if predicted_d is not None:
            np.testing.assert_array_equal(day['assets'], predicted_d['assets'][row])
            sigma = day['Lambda'] @ day['F'] @ day['Lambda'].T + np.diag(predicted_d['d'][row])
        if not np.isfinite(mu).all() or not np.isfinite(sigma).all():
            raise ValueError('FINANCIAL_MODEL_FINITE')
        if np.max(np.abs(sigma - sigma.T)) > 1e-8 or np.diag(sigma).min() < 0:
            raise ValueError('FINANCIAL_RISK_AXIS')
        previous = np.array([held_values.get(int(a), 0.) / nav for a in axis])
        eligible = base.entry_eligibility(day, pending, doc['history_observations_min'])
        atom = 'signal_' + str(index)
        budget.admit(atom, 'execution', {'cpu_seconds': job['solver_cpu_cap'] + job['overhead_cpu_seconds'] / job['solver_jobs'] * .8,
                                       'solver_cpu_seconds': job['solver_cpu_cap'], 'solver_jobs': 1})
        tick = time.process_time()
        x, solver = base.solve(mu, sigma, 5, float(lam), job['cost'], previous, eligible, job['solver_cpu_cap'], 4)
        elapsed = time.process_time() - tick
        if elapsed > job['solver_cpu_cap']:
            raise ValueError('ATOMIC_SOLVER_CPU_CAP')
        marginal = -mu / 5 + float(lam) * np.diag(sigma) / 25 + job['cost'] * (np.abs(.2 - previous) - previous)
        priority = sorted(np.flatnonzero(x), key=lambda p: (float(marginal[p]), str(day['assets'][p])))
        outside = sum(v for a, v in held_values.items() if a not in set(map(int, axis))) / nav
        common_score = base.objective(x, classic['mu'][row], factor_sigma, 5, float(lam), job['cost'], previous) + job['cost'] * outside
        budget.finish(atom, {'solver_cpu_seconds': elapsed, 'solver_jobs': 1})
        return {'signal_index': index, 'signal_date': str(data['dates'][index]),
            'priority_global': [int(axis[p]) for p in priority], 'target_assets': [str(day['assets'][p]) for p in priority],
            'signal_nav': nav, 'eligible_count': int(eligible.sum()),
            'previous_weights_signal': [{'asset_index': int(a), 'weight': v / nav} for a, v in held_values.items()],
            'outside_active_axis_weight': outside, 'solver': solver, 'mu_sha256': identity(mu.tolist()),
            'Sigma_sha256': identity(sigma.tolist()), 'day_sha256': record['sha256'],
            'common_coefficient_objective': common_score,
            'common_objective_scope': 'classical mu and factor Sigma; event-aware actual previous holdings and bounded solver'}

    result = simulate_path(market, job['signal_indices'], planner, job['cost'], events, 5, tail=True)
    budget.save()
    if budget.used['cpu_seconds'] > cap['cpu_seconds'] or resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 > job['memory_mib_cap']:
        raise ValueError('FINANCIAL_PATH_RESOURCE_CAP')
    path_sha = write_json(out / 'ledger.json', result)
    records = result['interval_records']
    series_sha = base.save_npz(out / 'series.npz', date_indices=np.array(job['signal_indices']),
        net_returns=np.array([r['net_return'] for r in records]), net_losses=np.array([r['net_loss'] for r in records]),
        cost_fractions=np.array([r['cost_fraction'] for r in records]), turnover_fractions=np.array([r['turnover_fraction'] for r in records]),
        mdd_open_returns=np.array([r['open_to_open_after_entry_return'] for r in records]),
        initial_net_factor=np.array(result['opening_records'][0]['nav_after']))
    budget.save()
    if budget.used['cpu_seconds'] > cap['cpu_seconds']:
        raise ValueError('FINANCIAL_OUTPUT_CPU_CAP')
    write_json(out / 'result.json', {'schema_version': 'qf.stock-finance-corporate-action-path.v2', 'job': job,
        'lambda': lam, 'selection_sha256': selection_sha, 'finance_freeze_sha256': digest(finance_root / 'freeze.json'),
        'event_manifest_sha256': doc['event_manifest_sha256'], 'legacy_finance_freeze_sha256': doc['legacy_finance_freeze_sha256'],
        'data_epoch': doc['data_epoch'], 'task_id': doc['task_id'], 'run_id': doc['campaign_id'] + '/' + job_id,
        'campaign_id': doc['campaign_id'], 'source_commit': doc['source_commit'], 'ledger_sha256': path_sha,
        'series_sha256': series_sha, 'resource_ledger_sha256': digest(budget.path), 'summary': result['summary'],
        'prediction_source': prediction_record, 'common_prediction_source': classic_record, 'risk_head_manifest_sha256': risk_manifest_sha,
        'execution_mode': doc['execution_mode'], 'scientific_status': 'LOCAL_VERIFIED',
        'hardware_calls': 0, 'new_quantum_calls': 0, 'status': 'COMPLETE'})
    return {'job_id': job_id, 'status': 'COMPLETE', 'sha256': digest(out / 'result.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('freeze')
    for flag in ['stock-root', 'data', 'legacy-root', 'events', 'supervisors', 'output']:
        p.add_argument('--' + flag, type=Path, required=True)
    p = sub.add_parser('run-path')
    for flag in ['stock-root', 'data', 'finance-root']:
        p.add_argument('--' + flag, type=Path, required=True)
    p.add_argument('--job-id', required=True)
    args = parser.parse_args()
    if args.mode == 'freeze':
        output = freeze(args.stock_root, args.data, args.legacy_root, args.events, args.supervisors, args.output)
    else:
        output = run_path(args.stock_root, args.data, args.finance_root, args.job_id)
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
