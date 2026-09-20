"""Expanded parameter, serial batch, filtering and comparison notebook controls."""
from __future__ import annotations
import html
import json
import math

import ipywidgets as widgets


def _html(value):
    return html.escape(str(value))


def _notify_error(session, error):
    session.status_message = str(error)+'。请核对配置字段、取值范围与历史运行数量。'
    refresh_widgets(session)


def build_controls(session):
    items = {}
    style = {'description_width': '140px'}
    layout = widgets.Layout(width='430px')
    options = session.current_config
    integer = {'seed': ('随机种子', 0, 2**32-1), 'shots': ('每设置 shots', 1, 100000), 'epochs': ('训练轮数', 1, 200)}
    for name, (label, low, high) in integer.items():
        if name in options:
            items[name] = widgets.BoundedIntText(value=options[name], min=low, max=high, description=label, style=style, layout=layout)
    if 'risk_mode' in options:
        items['risk_mode'] = widgets.Dropdown(options=[('因子风险', 'factor'), ('收缩协方差', 'shrinkage'),
                                                       ('量子特定风险', 'quantum_specific')],
                                               value=options['risk_mode'], description='风险估计', style=style, layout=layout)
    if 'risk_lambda' in options:
        items['risk_lambda'] = widgets.BoundedFloatText(value=options['risk_lambda'], min=0, max=100, step=.1,
                                                        description='风险系数 λ', style=style, layout=layout)
        items['cost_bps'] = widgets.BoundedFloatText(value=options['cost']*10000, min=0, max=100, step=1,
                                                     description='交易成本（基点）', style=style, layout=layout)
    if 'qaoa_mode' in options:
        items['qaoa_mode'] = widgets.Dropdown(options=[('固定参数', 'fixed'), ('COBYLA 优化', 'optimize')],
                                               value=options['qaoa_mode'], description='QAOA 求解模式', style=style, layout=layout)
        for name, symbol in [('qaoa_gamma', 'γ'), ('qaoa_beta', 'β')]:
            items[name] = widgets.BoundedFloatText(value=options[name], min=-math.pi, max=math.pi, step=.05,
                                                    description=symbol, style=style, layout=layout)
    parameter_widgets = list(items.values())
    items['configuration'] = widgets.HTML()
    items['status'] = widgets.HTML()
    items['run'] = widgets.Button(description='运行当前配置', icon='play', button_style='primary')
    items['default'] = widgets.Button(description='恢复默认配置', icon='undo')
    items['date'] = widgets.Dropdown(options=[('全部日期', None)], description='日期', disabled=True, style=style, layout=layout)
    items['asset'] = widgets.Dropdown(options=[('全部资产', None)], description='资产', disabled=True, style=style, layout=layout)
    items['candidate'] = widgets.Dropdown(options=[('全部候选', None)], description='候选', disabled=True, style=style, layout=layout)
    session._widgets = items

    def change(name, event):
        if event['name'] != 'value' or getattr(session, '_refreshing_widgets', False):
            return
        try:
            session.prepare_configuration({'cost' if name == 'cost_bps' else name:
                                           event['new']/10000 if name == 'cost_bps' else event['new']})
        except Exception as error:
            _notify_error(session, error)
    for name in [k for k in items if k in options or k == 'cost_bps']:
        items[name].observe(lambda event, key=name: change(key, event), names='value')
    def reset(_):
        session.prepare_configuration(reset=True)
        session._refreshing_widgets = True
        try:
            for name, value in session.default_config.items():
                items['cost_bps' if name == 'cost' else name].value = value*10000 if name == 'cost' else value
        finally:
            session._refreshing_widgets = False
        refresh_widgets(session)
    items['default'].on_click(reset)
    def run(_):
        try:
            session._start_background([session.current_config])
        except Exception as error:
            _notify_error(session, error)
    items['run'].on_click(run)
    def filter_change(event):
        if not getattr(session, '_refreshing_widgets', False) and event['name'] == 'value':
            try:
                session.render_current()
            except Exception as error:
                _notify_error(session, error)
    for name in ('date', 'asset', 'candidate'):
        items[name].observe(filter_change, names='value')
    explanation = '更改参数形成待运行配置；点击运行时冻结配置快照，并建立新的运行身份。1 基点 = 0.0001。'
    if 'qaoa_mode' in options:
        explanation += ' 固定模式使用填写的角度；优化模式将其作为 COBYLA 初值，最多求值 18 次。'
    if session.topic == '04':
        explanation += ' 当前单层线路由计算基态出发，首个代价层的 γ 贡献全局相位；β 控制当前概率变化。'
    children = [widgets.HTML('<b>运行参数</b><p>'+explanation+'</p>'), *parameter_widgets,
                items['configuration'], widgets.HBox([items['run'], items['default']]), items['status'],
                widgets.HTML('<b>结果筛选</b><p>筛选读取已完成运行的保存工件。</p>'),
                items['date'], items['asset'], items['candidate'], session.interactive_output]
    box = widgets.VBox(children)
    refresh_widgets(session)
    return box


def build_comparison_controls(session):
    supported = 'risk_lambda' in session.default_config
    choices = [('三行自定义配置', 'custom')]
    if supported:
        choices = [('风险系数：0.1 / 1 / 10', 'risk'), ('交易成本：0 / 10 / 50 基点', 'cost'), *choices]
    items = {
        'preset': widgets.Dropdown(options=choices, description='批次预设', style={'description_width': '120px'}, layout=widgets.Layout(width='460px')),
        'custom_1': widgets.Text(value='{}', description='第 1 组 JSON', layout=widgets.Layout(width='90%')),
        'custom_2': widgets.Text(value='{}', description='第 2 组 JSON', layout=widgets.Layout(width='90%')),
        'custom_3': widgets.Text(value='{}', description='第 3 组 JSON', layout=widgets.Layout(width='90%')),
        'batch': widgets.Button(description='串行运行所选批次', icon='play', layout=widgets.Layout(width='230px')),
        'history': widgets.SelectMultiple(options=[], description='历史运行', rows=4, layout=widgets.Layout(width='95%')),
        'compare': widgets.Button(description='比较所选 2—3 次运行', icon='table', layout=widgets.Layout(width='230px')),
        'output': widgets.Output(),
        'status': widgets.HTML(),
    }
    session._comparison_widgets = items
    def run_batch(_):
        try:
            base = session.current_config
            if items['preset'].value == 'risk':
                configs = [dict(base, risk_lambda=value) for value in [.1, 1., 10.]]
            elif items['preset'].value == 'cost':
                configs = [dict(base, cost=value) for value in [0., .001, .005]]
            else:
                configs = []
                for name in ['custom_1', 'custom_2', 'custom_3']:
                    if items[name].value.strip():
                        item = json.loads(items[name].value)
                        if not isinstance(item, dict):
                            raise ValueError('每行 JSON 必须是一个参数对象')
                        configs.append(dict(base, **item))
            session._start_background(configs)
        except Exception as error:
            _notify_error(session, error)
    items['batch'].on_click(run_batch)
    def compare(_):
        try:
            chosen = [view for view in session.history if view.run_id in items['history'].value]
            from .notebook_display import show_comparison
            with items['output']:
                items['output'].clear_output(wait=True)
                show_comparison(chosen)
        except Exception as error:
            _notify_error(session, error)
    items['compare'].on_click(compare)
    box = widgets.VBox([widgets.HTML('<b>小规模串行对照</b><p>每批 1—3 组；一组完成后再开始下一组。自定义行使用成本小数，例如 {"cost": 0.001}。</p>'),
                        items['preset'], items['custom_1'], items['custom_2'], items['custom_3'], items['batch'],
                        widgets.HTML('<b>已完成运行对比</b><p>选择 2—3 次历史运行。每次保留自己的结果和配置。</p>'),
                        items['history'], items['compare'], items['status'], items['output']])
    refresh_widgets(session)
    return box


def refresh_widgets(session):
    if getattr(session, '_refreshing_widgets', False):
        return
    session._refreshing_widgets = True
    try:
        items = session._widgets
        if items:
            current = session.current_config
            items['configuration'].value = '<b>待运行配置</b><pre>'+_html(json.dumps(current, ensure_ascii=False, indent=2))+'</pre>'
            items['status'].value = '<b>状态：'+_html(session.status)+'</b><p>'+_html(session.status_message)+'</p>'
            items['run'].disabled = session.busy
            if 'qaoa_mode' in current:
                word = '初值' if current['qaoa_mode'] == 'optimize' else '固定值'
                items['qaoa_gamma'].description = 'γ '+word+'（弧度）'
                items['qaoa_beta'].description = 'β '+word+'（弧度）'
            view = session.current
            if view is not None and getattr(session, '_filter_run_id', None) != view.run_id:
                panel = view.documents.get('input', {})
                risk = view.documents.get('risk_artifact', {})
                dates = (risk.get('train_dates', []) + risk.get('evaluation_dates', [])
                         if session.topic == '02'
                         else panel.get('dates', risk.get('evaluation_dates', [])))
                assets = panel.get('asset_order', risk.get('asset_order', view.documents.get('bridge_instance', {}).get('asset_order', [])))
                names = view.arrays.get('quantum_probabilities', {}).get('candidate_names', [])
                if not len(names) and 'metrics' in view.tables:
                    names = view.tables['metrics']['candidate'].tolist()
                for key, label, values in [('date', '日期', dates), ('asset', '资产', assets), ('candidate', '候选', names)]:
                    values = list(dict.fromkeys(map(str, values)))
                    items[key].options = [('全部'+label, None)]+[(v, v) for v in values]
                    items[key].value = None
                    items[key].disabled = not bool(values)
                session._filter_run_id = view.run_id
        comparison = session._comparison_widgets
        if comparison:
            options = [(view.run_id+' | λ='+str(view.configuration.get('risk_lambda', '归档'))+
                        ' | cost='+str(view.configuration.get('cost', '归档')), view.run_id) for view in session.history]
            if list(comparison['history'].options) != options:
                comparison['history'].options = options
                comparison['history'].value = tuple(view.run_id for view in session.history[-2:]) if len(session.history) >= 2 else ()
            comparison['batch'].disabled = session.busy
            comparison['compare'].disabled = len(session.history) < 2
            comparison['status'].value = '<p>'+_html(session.status_message)+'</p>'
    finally:
        session._refreshing_widgets = False
