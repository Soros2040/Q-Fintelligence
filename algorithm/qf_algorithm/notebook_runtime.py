"""Isolated, serial local calculations behind the eight teaching notebooks."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import copy
import datetime as dt
import json
from pathlib import Path
import threading
import time
import uuid

import pandas as pd

from .api import digest, environment, identity, load_json, run_topic, write_json
from .config import RunConfig, validate_compute_parameters
from .data import load_panel, validate_panel
from .notebook_audit import RunView, load_run
from .paths import DataLayout, PACKAGE
from .pipeline import run_pipeline

PIPELINE_TOPICS = {'00', '02', '05', '07'}


class NotebookSession:
    """Preserve completed views while users prepare and run fresh configurations."""
    def __init__(self, topic='00', data_root=None, output_root=None, renderer=None):
        from ipywidgets import Output
        self.topic = str(topic).zfill(2)
        if self.topic not in {f'{i:02d}' for i in range(8)}:
            raise ValueError('NOTEBOOK_TOPIC_00_TO_07')
        self.layout = DataLayout.resolve(data_root)
        self.data_root = self.layout.root
        self.output_root = Path(output_root or Path.cwd()/'outputs'/'notebooks').resolve()
        if self.layout.protected(self.output_root):
            raise ValueError('OUTPUT_OVERLAPS_IMPLEMENTATION_OR_ARCHIVE')
        self.renderer = renderer
        self.interactive_output = Output()
        self.history: list[RunView] = []
        self.current: RunView | None = None
        self.status = 'IDLE'
        self.status_message = '默认配置已就绪。运行后可查看本次计算的完整结果。'
        self.last_error = None
        self.last_display_error = None
        self.last_request = None
        self._lock = threading.Lock()
        self._busy = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='qf-notebook-'+self.topic)
        self._future = None
        self._controls = None
        self._comparison_controls = None
        self._widgets = {}
        self._comparison_widgets = {}
        self._default = self._make_default()
        self._pending = copy.deepcopy(self._default)
        self._pending_revision = 0
        self._running_revision = 0

    def _make_default(self):
        if self.topic in PIPELINE_TOPICS:
            return {'seed': 2026090902, 'shots': 1024, 'epochs': 40,
                    'risk_mode': 'quantum_specific' if self.topic == '02' else 'factor',
                    'risk_lambda': 1., 'cost': .001, 'qaoa_gamma': .6, 'qaoa_beta': .25,
                    'qaoa_mode': 'optimize'}
        if self.topic in {'03', '04'}:
            instance = load_json(self.layout.campaign/'quantum_freeze.json')['instances'][0]
            options = {'seed': 2026090801, 'risk_lambda': instance['lambda'], 'cost': instance['cost']}
            if self.topic == '04':
                options.update(shots=1024, qaoa_gamma=.6, qaoa_beta=.25, qaoa_mode='fixed')
            return options
        return {}

    @property
    def default_config(self):
        return copy.deepcopy(self._default)

    @property
    def current_config(self):
        """A snapshot of pending controls; changing it cannot alter completed views."""
        return copy.deepcopy(self._pending)

    @property
    def busy(self):
        return self._busy

    @property
    def pending_future(self):
        return self._future

    def prepare_configuration(self, config=None, *, reset=False):
        """Validate the next configuration and publish its pending state."""
        options = self.default_config if reset else self.current_config
        if config is not None:
            if not isinstance(config, dict):
                raise ValueError('NOTEBOOK_CONFIG_OBJECT_REQUIRED')
            options.update(copy.deepcopy(config))
        snapshot = self._validated(options)
        with self._lock:
            self._pending = snapshot
            self._pending_revision += 1
            if self._busy:
                self.status = 'RUNNING'
                self.status_message = '正在按已冻结的配置计算；下次配置已更新，等待本次计算完成后运行。'
            else:
                self.status = 'PENDING'
                self.status_message = ('默认配置已恢复，等待运行。' if reset else
                                       '待运行配置已更新。点击“运行当前配置”生成新的计算记录。')
        self._refresh_widgets()
        return self.current_config

    def _validated(self, config=None):
        if config is not None and not isinstance(config, dict):
            raise ValueError('NOTEBOOK_CONFIG_OBJECT_REQUIRED')
        result = self.default_config
        result.update(self.current_config if config is None else copy.deepcopy(config))
        if set(result)-set(self._default):
            raise ValueError('NOTEBOOK_PARAMETERS_NOT_APPLICABLE:'+','.join(sorted(set(result)-set(self._default))))
        if self.topic in PIPELINE_TOPICS:
            RunConfig(**result).validate()
        elif self.topic in {'03', '04'}:
            if type(result['seed']) is not int or not 0 <= result['seed'] < 2**32:
                raise ValueError('CONFIG_UNSIGNED_32BIT_SEED')
            if self.topic == '04' and (type(result['shots']) is not int or not 1 <= result['shots'] <= 100000):
                raise ValueError('CONFIG_SHOTS_RANGE')
            validate_compute_parameters(result['risk_lambda'], result['cost'], result.get('qaoa_gamma', .6),
                                        result.get('qaoa_beta', .25), result.get('qaoa_mode', 'fixed'))
        return result

    def run_default(self) -> RunView:
        return self.run(self.default_config)

    def run(self, config=None) -> RunView:
        snapshot = self._validated(config)
        self._reserve()
        return self._execute_reserved([snapshot])[0]

    def run_batch(self, configs) -> list[RunView]:
        snapshots = self._batch_snapshots(configs)
        self._reserve()
        return self._execute_reserved(snapshots)

    def _batch_snapshots(self, configs):
        if not isinstance(configs, (list, tuple)) or not 1 <= len(configs) <= 3:
            raise ValueError('NOTEBOOK_BATCH_ONE_TO_THREE_CONFIGURATIONS')
        return [self._validated(config) for config in configs]

    def _reserve(self):
        with self._lock:
            if self._busy:
                raise RuntimeError('NOTEBOOK_RUN_ALREADY_IN_PROGRESS')
            self._busy = True
            self._running_revision = self._pending_revision
        self.status = 'RUNNING'
        self.last_error = None
        self.status_message = '配置快照已冻结，正在执行本地计算。'
        self._refresh_widgets()

    def _start_background(self, configs):
        """Reserve before starting a worker, so repeated clicks share the in-flight job."""
        with self._lock:
            if self._busy:
                return self._future
            snapshots = self._batch_snapshots(configs)
            self._busy = True
            self._running_revision = self._pending_revision
            self.status = 'RUNNING'
            self.last_error = None
            self.status_message = '配置快照已冻结，正在执行本地计算。'
            self._future = self._executor.submit(self._execute_reserved, snapshots)
        self._refresh_widgets()
        return self._future

    def _execute_reserved(self, snapshots):
        results = []
        try:
            for index, config in enumerate(snapshots, 1):
                with self._lock:
                    self.status_message = f'正在串行运行第 {index} / {len(snapshots)} 组。'
                    if self._pending_revision != self._running_revision:
                        self.status_message += ' 下次配置已更新，等待本次计算完成后运行。'
                self._refresh_widgets()
                view = self._calculate(config)
                self.current = view
                self.history.append(view)
                results.append(view)
                self._refresh_widgets()
                self.last_display_error = None
                try:
                    self.render_current()
                except Exception as error:
                    self.last_display_error = str(error)
                self._refresh_widgets()
            return results
        except Exception as error:
            self.status = 'FAILED'
            self.last_error = {'type': type(error).__name__, 'message': str(error),
                               'suggestion': '请核对参数范围、输入目录与运行环境，再以新的运行身份重试。'}
            self.status_message = '本次计算失败：'+str(error)[:350]+'。'+self.last_error['suggestion']
            if self.last_request:
                write_json(self.output_root/'_requests'/(self.last_request['run_id']+'.failure.json'),
                           {'status': 'FAILED', 'request_sha256': digest(self.output_root/'_requests'/(self.last_request['run_id']+'.json')), **self.last_error})
            raise
        finally:
            with self._lock:
                self._busy = False
                if self.last_error is None:
                    pending = self._pending_revision != self._running_revision
                    self.status = 'PENDING' if pending else 'COMPLETE'
                    self.status_message = f'已完成 {len(results)} 组计算；历史记录共 {len(self.history)} 次。'
                    if pending:
                        self.status_message += ' 待运行配置已更新。点击“运行当前配置”生成新的计算记录。'
                    if self.last_display_error:
                        self.status_message += ' 图表显示需要核对：'+self.last_display_error[:250]
                else:
                    self.status = 'FAILED'
                    self.status_message = '本次计算失败：'+self.last_error['message'][:350]+'。'+self.last_error['suggestion']
            self._refresh_widgets()

    def _calculate(self, config):
        started = time.perf_counter()
        self.last_request = None
        request_id = uuid.uuid4()
        run_id = 'notebook_'+self.topic+'_'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+request_id.hex
        implementation = {str(p.relative_to(PACKAGE)): digest(p) for p in sorted(PACKAGE.rglob('*.py'))}
        request = {'schemaVersion': 'qf.notebook-request.v1', 'run_id': run_id, 'topic': self.topic,
                   'request_id': str(request_id),
                   'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'configuration': config,
                   'data_root': str(self.data_root), 'implementation': implementation,
                   'implementation_sha256': identity(implementation), 'external_calls': 0,
                   'execution': 'LOCAL_SERIAL_SOFTWARE_CALCULATION'}
        self.last_request = request
        request_path = self.output_root/'_requests'/(run_id+'.json')
        write_json(request_path, request)
        destination = self.output_root/run_id
        if self.topic in PIPELINE_TOPICS:
            result = run_pipeline(data_root=self.data_root, output_dir=self.output_root, run_id=run_id,
                                  frozen=self.topic == '07', **config)
            destination = Path(result['output_dir'])
        elif self.topic in {'03', '04'}:
            bundle = run_topic('bridge' if self.topic == '03' else 'qaoa', data_root=self.data_root,
                               output_dir=self.output_root, run_id=run_id, **config)
            destination = bundle.output_dir
        elif self.topic == '01':
            panel = load_panel(frozen=True, data_root=self.data_root)
            validate_panel(panel)
            destination.mkdir(parents=True, exist_ok=False)
            write_json(destination/'input.json', panel)
            self._write_wrapper(destination, run_id, config, implementation,
                                {'status': 'COMPLETE', 'dates': len(panel['dates']), 'assets': len(panel['asset_order']),
                                 'data_epoch': panel['data_epoch'], 'input_sha256': digest(destination/'input.json')},
                                panel['parents'], panel['data_epoch'], 'LOCAL_FROZEN_INPUT_NORMALIZATION')
        else:
            from .analysis import recompute_primary_statistics
            report = recompute_primary_statistics(self.data_root, output_dir=destination)
            pd.DataFrame(report['comparisons']).to_csv(destination/'comparisons.csv', index=False)
            summary = {key: report[key] for key in ['status', 'comparison_count', 'maximum_numeric_error', 'external_calls', 'scope']}
            self._write_wrapper(destination, run_id, config, implementation, summary, report['parents'],
                                'FROZEN_SOURCE_EPOCHS', 'EXISTING_RESULTS_REANALYSIS')
        # The request is copied byte-for-byte after the completed backend has created its directory.
        (destination/'notebook_request.json').write_bytes(request_path.read_bytes())
        result = load_json(destination/'result.json')
        result['notebook'] = {'topic': self.topic, 'request': 'notebook_request.json',
                              'request_sha256': digest(request_path), 'wall_seconds': time.perf_counter()-started}
        result['artifacts'] = [{'path': p.name, 'sha256': digest(p), 'bytes': p.stat().st_size}
                               for p in sorted(destination.iterdir()) if p.is_file() and p.name not in {'result.json', 'result.sha256'}]
        write_json(destination/'result.json', result)
        (destination/'result.sha256').write_text(digest(destination/'result.json')+'  result.json\n', encoding='utf-8')
        return load_run(destination)

    def _write_wrapper(self, destination, run_id, config, implementation, summary, parents, data_epoch, execution_mode):
        baseline = load_json(PACKAGE/'provenance/delivery_baseline.json')
        write_json(destination/'result.json', {'schemaVersion': 'qf.software-result.v2', 'status': 'COMPLETE',
                   'runId': run_id, 'dataEpoch': data_epoch, 'experimentId': 'SOFTWARE_NOTEBOOK_'+self.topic,
                   'sourceCommit': baseline['workspaceCommit'], 'workspaceCommit': baseline['workspaceCommit'],
                   'configuration': config, 'implementation': implementation, 'environment': environment(),
                   'executionMode': execution_mode, 'scientificStatus': 'LOCAL_VERIFIED',
                   'archiveStatus': 'RESULT_GENERATED', 'releaseStatus': 'LOCAL_REVIEW',
                   'summary': summary, 'parents': parents, 'external_calls': 0})

    def controls(self):
        if self._controls is None:
            from .notebook_widgets import build_controls
            self._controls = build_controls(self)
        return self._controls

    def comparison_controls(self):
        if self._comparison_controls is None:
            from .notebook_widgets import build_comparison_controls
            self._comparison_controls = build_comparison_controls(self)
        return self._comparison_controls

    def render_current(self, selection=None):
        if self.current is None:
            return
        renderer = self.renderer
        if renderer is None:
            from .notebook_display import render_run
            renderer = render_run
        if selection is None and self._widgets:
            selection = {name: self._widgets[name].value for name in ('date', 'asset', 'candidate')
                         if name in self._widgets and self._widgets[name].value is not None}
        with self.interactive_output:
            self.interactive_output.clear_output(wait=True)
            renderer(self.current, self.topic, selection=selection or None)

    def _refresh_widgets(self):
        if self._widgets or self._comparison_widgets:
            from .notebook_widgets import refresh_widgets
            refresh_widgets(self)

    def close(self):
        """Release the session's worker after any active calculation has finished."""
        self._executor.shutdown(wait=False)
